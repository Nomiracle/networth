"""MCP 服务真实运行测试（不依赖真实后端与外网）。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
PROJECT_ROOT = BACKEND.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mcp import server as mcp_server  # noqa: E402
from mcp import tools as mcp_tools  # noqa: E402


# ---------------------------------------------------------------- 假后端

class FakeBackendHandler(BaseHTTPRequestHandler):
    """可配置的假后端，记录请求并按表返回。"""
    # 类变量：路由 -> (status, payload)
    routes: dict[tuple[str, str], tuple[int, dict]] = {}
    requests: list[dict] = []
    lock = threading.Lock()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path
        with self.lock:
            self.__class__.requests.append({"method": method, "path": self.path, "headers": dict(self.headers)})
        # 读取 body（若有）
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        # 查找精确匹配或前缀匹配
        key = (method, path)
        # 尝试带 query 的匹配也按 path 匹配
        route = self.__class__.routes.get(key)
        if route is None:
            # 模糊：查找 path 前缀匹配（用于动态 id）
            for (m, p), val in self.__class__.routes.items():
                if m == method and path.startswith(p):
                    route = val
                    break
        if route is None:
            # 默认 404
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"code": "not_found", "message": f"fake 未配置 {method} {path}"}}).encode("utf-8"))
            return
        status, payload = route
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def log_message(self, format, *args):
        pass


class FakeBackend:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict]] | None = None):
        FakeBackendHandler.routes = routes or {}
        FakeBackendHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackendHandler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def set_routes(self, routes: dict[tuple[str, str], tuple[int, dict]]):
        with FakeBackendHandler.lock:
            FakeBackendHandler.routes = routes

    def clear_requests(self):
        with FakeBackendHandler.lock:
            FakeBackendHandler.requests = []

    @property
    def requests(self):
        with FakeBackendHandler.lock:
            return list(FakeBackendHandler.requests)


# ---------------------------------------------------------------- 测试

class McpProtocolTests(unittest.TestCase):

    def test_09_set_location_never_uses_full_snapshot_put(self):
        """set_location 只能走地点窄接口：绝不发出不带 items 的整期写（那会清空该期明细）。"""
        os.environ["NETWORTH_MCP_ALLOW_WRITE"] = "1"
        fake = FakeBackend()
        old_base = os.environ.get("NETWORTH_API_BASE")
        old_token = os.environ.get("NETWORTH_MCP_TOKEN")
        os.environ["NETWORTH_API_BASE"] = fake.base
        os.environ["NETWORTH_MCP_TOKEN"] = "fake-token"
        try:
            fake.set_routes({
                ("PUT", "/api/snapshots/2025-09-03/location"): (200, {"snapshot": {"date": "2025-09-03", "location": {"text": "SynthCityX", "source": "manual"}}}),
                ("GET", "/api/snapshots/2025-09-03"): (200, {"snapshot": {"date": "2025-09-03", "location": {"text": "SynthCityX", "source": "manual"}, "marker": "LOC_OK"}, "items": []}),
            })
            fake.clear_requests()
            resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": "set_location", "arguments": {"date": "2025-09-03", "text": "SynthCityX", "source": "manual"}}})
            self.assertFalse(resp["result"].get("isError"))
            self.assertIn("LOC_OK", resp["result"]["content"][0]["text"])
            self.assertTrue(any(r["method"] == "PUT" and r["path"].endswith("/location") for r in fake.requests))
            bare = [r for r in fake.requests
                    if r["method"] in ("PUT", "POST", "PATCH")
                    and r["path"].rstrip("/").endswith("/api/snapshots/2025-09-03")]
            self.assertEqual(bare, [], f"不得对整期快照发出部分写请求（会清空明细）：{bare}")

            # 后端没有窄接口（全部 404）时必须失败，而不是退化到整期 PUT
            fake.set_routes({})
            fake.clear_requests()
            resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                              "params": {"name": "set_location", "arguments": {"date": "2025-09-03", "text": "SynthCityX", "source": "manual"}}})
            self.assertTrue(resp["result"].get("isError"))
            self.assertIn("不会退化到整期 PUT", resp["result"]["content"][0]["text"])
            bare = [r for r in fake.requests
                    if r["method"] in ("PUT", "POST", "PATCH")
                    and r["path"].rstrip("/").endswith("/api/snapshots/2025-09-03")]
            self.assertEqual(bare, [], f"失败路径同样不得退化到整期写：{bare}")
        finally:
            fake.close()
            os.environ.pop("NETWORTH_MCP_ALLOW_WRITE", None)
            if old_base is None:
                os.environ.pop("NETWORTH_API_BASE", None)
            else:
                os.environ["NETWORTH_API_BASE"] = old_base
            if old_token is None:
                os.environ.pop("NETWORTH_MCP_TOKEN", None)
            else:
                os.environ["NETWORTH_MCP_TOKEN"] = old_token

    def test_01_initialize_negotiation(self):
        """分别发 2025-11-25 / 2025-06-18 / 2024-11-05 → 均成功且回显协商版本。"""
        for ver in ("2025-11-25", "2025-06-18", "2024-11-05"):
            req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": ver, "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
            resp = mcp_server.handle_request(req)
            self.assertIsNotNone(resp)
            self.assertEqual(resp["result"]["protocolVersion"], ver, f"版本 {ver} 应原样回显")
            self.assertNotEqual(resp.get("error", {}).get("code"), -32000, "绝不能返回 -32000 Unsupported")
        # 未知版本也应成功（回退到最新）
        req = {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2099-01-01", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        resp = mcp_server.handle_request(req)
        self.assertIn("result", resp)
        self.assertEqual(resp["result"]["protocolVersion"], mcp_server.LATEST_VERSION)
        # 头部携带版本也应被接受
        req = {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        resp = mcp_server.handle_request(req, http_headers={"mcp-protocol-version": "2025-11-25"})
        self.assertEqual(resp["result"]["protocolVersion"], "2025-11-25")

    def test_02_tools_list_and_schema(self):
        """默认不含写工具；ALLOW_WRITE=1 时含三个写工具；每个 inputSchema 合法。"""
        # 默认
        os.environ.pop("NETWORTH_MCP_ALLOW_WRITE", None)
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        resp = mcp_server.handle_request(req)
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertNotIn("save_snapshot", names)
        self.assertNotIn("patch_account", names)
        self.assertNotIn("set_location", names)
        self.assertIn("networth_health", names)
        self.assertIn("list_accounts", names)
        # 校验每个 inputSchema 是合法 JSON Schema（至少有 type:object）
        for tool in resp["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            schema = tool["inputSchema"]
            self.assertEqual(schema.get("type"), "object")
            self.assertIn("properties", schema)
            # 能被 json 序列化
            json.dumps(schema)

        # 写模式
        os.environ["NETWORTH_MCP_ALLOW_WRITE"] = "1"
        try:
            resp = mcp_server.handle_request(req)
            names = {t["name"] for t in resp["result"]["tools"]}
            self.assertIn("save_snapshot", names)
            self.assertIn("patch_account", names)
            self.assertIn("set_location", names)
            # 总数 16
            self.assertEqual(len(resp["result"]["tools"]), 16)
            for tool in resp["result"]["tools"]:
                schema = tool["inputSchema"]
                self.assertEqual(schema.get("type"), "object")
                json.dumps(schema)
        finally:
            os.environ.pop("NETWORTH_MCP_ALLOW_WRITE", None)

    def test_03_unknown_method_and_invalid_params(self):
        """未知方法 -> -32601；tools/call 参数缺失或类型错 -> -32602。"""
        # 未知方法
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "unknown/method", "params": {}})
        self.assertEqual(resp["error"]["code"], -32601)
        # tools/call 参数缺失
        os.environ.pop("NETWORTH_MCP_ALLOW_WRITE", None)
        # get_account_series 缺少 account_id
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_account_series", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32602)
        # 类型错
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_account_series", "arguments": {"account_id": "not-an-int"}}})
        self.assertEqual(resp["error"]["code"], -32602)
        # 枚举错
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "journal", "arguments": {"scope": "bad"}}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_04_backend_error_isError(self):
        """后端返回 401 或 502 时：tools/call 返回 isError:true 且不回假数据。"""
        fake = FakeBackend(routes={
            ("GET", "/api/accounts"): (401, {"error": {"code": "unauthorized", "message": "令牌无效"}}),
            ("GET", "/api/healthz"): (502, {"error": {"code": "backend_error", "message": "bad gateway"}}),
        })
        old_base = os.environ.get("NETWORTH_API_BASE")
        old_token = os.environ.get("NETWORTH_MCP_TOKEN")
        os.environ["NETWORTH_API_BASE"] = fake.base
        os.environ["NETWORTH_MCP_TOKEN"] = "fake-token"
        try:
            # 401
            resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_accounts", "arguments": {}}})
            # tools/call 成功时走 result，错误时 isError:true
            self.assertIn("result", resp)
            result = resp["result"]
            self.assertTrue(result.get("isError"))
            text = result["content"][0]["text"]
            self.assertIn("401", text)
            self.assertIn("unauthorized", text)
            # 502
            fake.set_routes({("GET", "/api/healthz"): (502, {"error": {"code": "backend_error", "message": "bad gateway"}})})
            resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "networth_health", "arguments": {}}})
            result = resp["result"]
            self.assertTrue(result.get("isError"))
            self.assertIn("502", result["content"][0]["text"])
        finally:
            fake.close()
            if old_base is None:
                os.environ.pop("NETWORTH_API_BASE", None)
            else:
                os.environ["NETWORTH_API_BASE"] = old_base
            if old_token is None:
                os.environ.pop("NETWORTH_MCP_TOKEN", None)
            else:
                os.environ["NETWORTH_MCP_TOKEN"] = old_token

    def test_05_write_after_read_verification(self):
        """写工具写后读验证：用本地 fake 后端断言 PUT/POST 之后确实发出了 GET，且回读值出现在返回内容里。"""
        os.environ["NETWORTH_MCP_ALLOW_WRITE"] = "1"
        fake = FakeBackend()
        old_base = os.environ.get("NETWORTH_API_BASE")
        old_token = os.environ.get("NETWORTH_MCP_TOKEN")
        os.environ["NETWORTH_API_BASE"] = fake.base
        os.environ["NETWORTH_MCP_TOKEN"] = "fake-token"
        try:
            # ---- save_snapshot ----
            fake.set_routes({
                ("PUT", "/api/snapshots/2025-09-01"): (200, {"snapshot": {"date": "2025-09-01", "networth": 1000}, "items": []}),
                ("GET", "/api/snapshots/2025-09-01"): (200, {"snapshot": {"date": "2025-09-01", "networth": 1000, "verified_marker": "READBACK_2025-09-01"}, "items": []}),
            })
            fake.clear_requests()
            resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "save_snapshot", "arguments": {"date": "2025-09-01", "items": [{"account_id": 1, "amount_cny": 100}]}}})
            self.assertIn("result", resp)
            self.assertFalse(resp["result"].get("isError"))
            paths = [r["path"] for r in fake.requests]
            self.assertTrue(any("PUT" in r["method"] or "POST" in r["method"] for r in fake.requests), f"应有写请求，实际 {paths}")
            self.assertTrue(any(r["method"] == "GET" and "2025-09-01" in r["path"] for r in fake.requests), f"应有 GET 回读，实际 {paths}")
            # 回读值出现在返回内容
            text = resp["result"]["content"][0]["text"]
            self.assertIn("READBACK_2025-09-01", text)
            self.assertIn("CNY", text)  # 金额标明币种

            # ---- patch_account ----
            fake.set_routes({
                ("PATCH", "/api/accounts/1"): (200, {"id": 1, "name": "新名"}),
                ("GET", "/api/accounts"): (200, {"accounts": [{"id": 1, "name": "新名", "patched_marker": "PATCH_READBACK"}]}),
            })
            fake.clear_requests()
            resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "patch_account", "arguments": {"id": 1, "name": "新名"}}})
            self.assertFalse(resp["result"].get("isError"))
            self.assertTrue(any(r["method"] == "PATCH" for r in fake.requests))
            self.assertTrue(any(r["method"] == "GET" for r in fake.requests))
            self.assertIn("PATCH_READBACK", resp["result"]["content"][0]["text"])

            # ---- set_location ----
            fake.set_routes({
                ("POST", "/api/snapshots/2025-09-02/location"): (200, {"ok": True}),
                ("GET", "/api/snapshots/2025-09-02"): (200, {"snapshot": {"date": "2025-09-02", "location": {"text": "测试地点"}, "marker": "LOC_READBACK"}, "items": []}),
            })
            fake.clear_requests()
            resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "set_location", "arguments": {"date": "2025-09-02", "text": "测试地点", "lat": 39.9, "lon": 116.3}}})
            self.assertFalse(resp["result"].get("isError"))
            self.assertTrue(any("location" in r["path"] for r in fake.requests))
            self.assertTrue(any(r["method"] == "GET" and "2025-09-02" in r["path"] for r in fake.requests))
            self.assertIn("LOC_READBACK", resp["result"]["content"][0]["text"])
        finally:
            fake.close()
            os.environ.pop("NETWORTH_MCP_ALLOW_WRITE", None)
            if old_base is None:
                os.environ.pop("NETWORTH_API_BASE", None)
            else:
                os.environ["NETWORTH_API_BASE"] = old_base
            if old_token is None:
                os.environ.pop("NETWORTH_MCP_TOKEN", None)
            else:
                os.environ["NETWORTH_MCP_TOKEN"] = old_token

    def test_06_stdio_and_http_transport(self):
        """传输往返：stdio 一个真实往返 + HTTP 一个真实往返（含 /healthz 与 /mcp）。"""
        # ---- stdio ----
        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp.server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(PROJECT_ROOT),
        )
        try:
            req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}) + "\n"
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(req)
            proc.stdin.flush()
            line = proc.stdout.readline()
            self.assertTrue(line.strip(), "stdio 应返回一行 JSON")
            resp = json.loads(line)
            self.assertEqual(resp["result"]["protocolVersion"], "2025-11-25")
            # ping
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            resp = json.loads(line)
            self.assertIn("result", resp)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

        # ---- HTTP ----
        # 起一个真实的 MCP HTTP 服务（随机端口）
        import socket
        from http.client import HTTPConnection

        # 找可用端口
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        # 启动服务线程
        http_server = ThreadingHTTPServer(("127.0.0.1", port), mcp_server.McpHttpHandler)
        http_server.daemon_threads = True
        t = threading.Thread(target=http_server.serve_forever, daemon=True)
        t.start()
        time.sleep(0.2)
        try:
            # GET /healthz
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/healthz")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(body["ok"])
            self.assertEqual(body["server"], "networth-mcp")
            conn.close()
            # POST /mcp initialize
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
            conn.request("POST", "/mcp", body=payload, headers={"Content-Type": "application/json", "Accept": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(body["result"]["protocolVersion"], "2025-11-25")
            conn.close()
            # POST with SSE Accept
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/mcp", body=payload, headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.getheader("Content-Type"))
            raw = resp.read().decode("utf-8")
            self.assertTrue(raw.startswith("data: "))
            data = json.loads(raw[len("data: "):].strip())
            self.assertEqual(data["result"]["protocolVersion"], "2025-11-25")
            conn.close()
        finally:
            http_server.shutdown()
            http_server.server_close()
            t.join(timeout=5)

    def test_07_no_direct_db_import(self):
        """MCP 包不得直连 DB：mcp/ 下不得出现 import sqlite3、不得打开 .db。"""
        import pathlib
        mcp_dir = PROJECT_ROOT / "mcp"
        for py in mcp_dir.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            self.assertNotIn("import sqlite3", text, f"{py.name} 不得 import sqlite3")
            self.assertNotIn("sqlite3.connect", text)
            self.assertNotIn(".db", text, f"{py.name} 不得出现 .db")

    def test_08_ping_and_notifications(self):
        """ping 返回；notifications/initialized 无响应。"""
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        self.assertIn("result", resp)
        resp = mcp_server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.assertIsNone(resp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
