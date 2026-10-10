"""净值管家 MCP 服务：JSON-RPC 2.0，纯标准库，STDIO + HTTP 双传输。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .tools import all_tools, dispatch_tool, get_tool

# ---------------------------------------------------------------- 协议版本

SUPPORTED_VERSIONS = ["2024-11-05", "2025-06-18", "2025-11-25"]
LATEST_VERSION = "2025-11-25"

def _negotiate(client_version: str | None) -> str:
    """健壮协商：接受客户端发来的任意已知版本，否则回最新；绝不因版本拒接。"""
    if client_version in SUPPORTED_VERSIONS:
        return client_version  # type: ignore
    if client_version is None or client_version == "":
        return LATEST_VERSION
    # 未知版本也回最新，兼容未来客户端（不返回 -32000）
    return LATEST_VERSION

# ---------------------------------------------------------------- JSON-RPC 工具

def _error_response(id_val: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_val, "error": {"code": code, "message": message}}

def _result_response(id_val: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_val, "result": result}

def handle_request(req: dict[str, Any], http_headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    """处理单条 JSON-RPC 请求，返回响应 dict；通知则返回 None。"""
    rpc_id = req.get("id")
    method = req.get("method")
    params = req.get("params") if isinstance(req.get("params"), dict) else {}
    if not isinstance(params, dict):
        params = {}

    # notifications/initialized 为通知，无响应
    if method == "notifications/initialized":
        return None

    if method == "initialize":
        # 兼容头部与 params 两种来源的 protocolVersion
        client_ver = params.get("protocolVersion")
        if http_headers:
            header_ver = http_headers.get("mcp-protocol-version") or http_headers.get("MCP-Protocol-Version")
            if header_ver and not client_ver:
                client_ver = header_ver
        negotiated = _negotiate(client_ver if isinstance(client_ver, str) else None)
        result = {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "networth-mcp", "version": "1.0.0"},
        }
        return _result_response(rpc_id, result)

    if method == "ping":
        return _result_response(rpc_id, {})

    if method == "tools/list":
        tools = all_tools()
        # 确保每个 inputSchema 是合法 JSON Schema（type:object）
        return _result_response(rpc_id, {"tools": tools})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if not isinstance(name, str) or not name:
            return _error_response(rpc_id, -32602, "缺少工具名 name")
        if not isinstance(arguments, dict):
            return _error_response(rpc_id, -32602, "arguments 必须是对象")
        tool = get_tool(name)
        if tool is None:
            return _error_response(rpc_id, -32601, f"未知工具: {name}")
        # 写工具未开放时同样视作未知（符合默认不含写工具的契约）
        allow_write = os.environ.get("NETWORTH_MCP_ALLOW_WRITE", "").strip() == "1"
        from .tools import WRITE_TOOLS
        if tool in WRITE_TOOLS and not allow_write:
            return _error_response(rpc_id, -32601, f"工具未启用: {name}")
        # 轻量校验，错误映射为 -32602
        from .tools import _validate_params
        schema = tool["inputSchema"]
        v_err = _validate_params(schema, arguments)
        if v_err:
            return _error_response(rpc_id, -32602, v_err)
        # 执行业务
        result = dispatch_tool(name, arguments)
        if "_validation_error" in result:
            return _error_response(rpc_id, -32602, str(result["_validation_error"]))
        # tools/call 成功时 result 为 {content,isError}
        return _result_response(rpc_id, result)

    # 未知方法
    return _error_response(rpc_id, -32601, f"未知方法: {method}")

# ---------------------------------------------------------------- STDIO 传输

def run_stdio() -> None:
    """STDIO 模式：每行一个 JSON-RPC 请求，逐行响应。"""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            # 无法解析时按 JSON-RPC 返回 -32700
            resp = _error_response(None, -32700, f"Parse error: {exc}")
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue
        # 支持批量（数组）与单条
        if isinstance(req, list):
            resps = []
            for item in req:
                r = handle_request(item)
                if r is not None:
                    resps.append(r)
            if resps:
                sys.stdout.write(json.dumps(resps, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        else:
            resp = handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()

# ---------------------------------------------------------------- HTTP 传输

class McpHttpHandler(BaseHTTPRequestHandler):
    """HTTP 处理：POST /mcp 与 GET /healthz。"""

    def log_message(self, format: str, *args: Any) -> None:
        # 仅在 NETWORTH_MCP_VERBOSE 时打印
        if os.environ.get("NETWORTH_MCP_VERBOSE", "").strip() == "1":
            sys.stderr.write(f"[networth-mcp] {self.address_string()} - {format % args}\n")

    def _set_cors(self) -> None:
        # 允许本地调试跨域
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, mcp-protocol-version, MCP-Protocol-Version, Accept")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._set_cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            body = json.dumps({
                "ok": True,
                "server": "networth-mcp",
                "readonly": os.environ.get("NETWORTH_MCP_ALLOW_WRITE", "").strip() != "1",
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": {"code": "not_found", "message": "未找到"}}, ensure_ascii=False).encode("utf-8"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/mcp", "/mcp/"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"code": "not_found", "message": "未找到"}}, ensure_ascii=False).encode("utf-8"))
            return
        # 鉴权：仅当配置了 NETWORTH_MCP_HTTP_TOKEN 时要求 Bearer
        http_token = os.environ.get("NETWORTH_MCP_HTTP_TOKEN", "").strip()
        if http_token:
            auth = self.headers.get("Authorization", "")
            if not auth.lower().startswith("bearer "):
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"code": "unauthorized", "message": "缺少 Bearer 令牌"}}, ensure_ascii=False).encode("utf-8"))
                return
            plain = auth.split(" ", 1)[1].strip()
            if plain != http_token:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"code": "unauthorized", "message": "令牌无效"}}, ensure_ascii=False).encode("utf-8"))
                return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        text = raw.decode("utf-8") if raw else ""
        # 收集可能的协议版本头
        header_map = {k.lower(): v for k, v in self.headers.items()}
        try:
            payload = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, ensure_ascii=False).encode("utf-8"))
            return
        # 支持单条与批量
        is_batch = isinstance(payload, list)
        requests = payload if is_batch else [payload]
        responses: list[dict[str, Any]] = []
        for req in requests:
            if not isinstance(req, dict):
                responses.append(_error_response(None, -32600, "Invalid Request"))
                continue
            resp = handle_request(req, http_headers=header_map)
            if resp is not None:
                responses.append(resp)
        if not responses:
            # 全部是通知，无响应体时返回 202
            self.send_response(202)
            self.end_headers()
            return
        out_obj: Any = responses if is_batch else responses[0]
        out_text = json.dumps(out_obj, ensure_ascii=False)
        out_bytes = out_text.encode("utf-8")
        # 若客户端 Accept 为 text/event-stream，则用 SSE 帧返回
        accept = self.headers.get("Accept", "")
        use_sse = "text/event-stream" in accept
        if use_sse:
            sse_body = f"data: {out_text}\n\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(sse_body)))
            self._set_cors()
            self.end_headers()
            self.wfile.write(sse_body)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self._set_cors()
            self.end_headers()
            self.wfile.write(out_bytes)

def run_http(bind: str, port: int) -> None:
    server = ThreadingHTTPServer((bind, port), McpHttpHandler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    host, real_port = server.server_address
    print(f"[networth-mcp] 监听 http://{host}:{real_port}/mcp  (healthz /healthz)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

# ---------------------------------------------------------------- 入口

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m mcp.server", description="networth-mcp JSON-RPC 服务")
    parser.add_argument("--stdio", action="store_true", help="STDIO 传输")
    parser.add_argument("--http", action="store_true", help="HTTP 传输")
    parser.add_argument("--bind", default=os.environ.get("NETWORTH_MCP_BIND", "127.0.0.1"), help="HTTP 绑定地址")
    parser.add_argument("--port", type=int, default=int(os.environ.get("NETWORTH_MCP_PORT", "18084")), help="HTTP 端口")
    args = parser.parse_args(argv)
    # 默认若未指定则根据参数判断；无参数时打印帮助
    if args.stdio:
        run_stdio()
        return 0
    if args.http:
        run_http(args.bind, args.port)
        return 0
    parser.print_help()
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
