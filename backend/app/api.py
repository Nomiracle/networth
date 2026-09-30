"""networth HTTP 接口层：手写路由 + Bearer 令牌鉴权（http.server ThreadingHTTPServer）。

错误统一 `{"error":{"code":…,"message":…}}`，HTTP 400/401/403/404/405/409/413/500。
除 `/api/healthz`、`/api/auth/register`、`/api/auth/login` 外都需要 `Authorization: Bearer <token>`。
非 /api 路径会尝试托管 NETWORTH_WEB_DIST 下的前端构建产物（SPA 回退 index.html）。
"""
from __future__ import annotations

import json
import mimetypes
import re
import socketserver
import sqlite3
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import domain
from .config import Settings
from .domain import DomainError
from .store import ConflictError, Store

MAX_BODY = 8 * 1024 * 1024  # 8MB 请求体上限

Ctx = Any  # 见 RequestContext


class RequestContext:
    """一次请求的上下文。"""

    def __init__(self, handler: "ApiHandler", params: dict[str, str]) -> None:
        self.handler = handler
        self.params = params
        self.settings: Settings = handler.settings
        self.store: Store = handler.store
        self.query: dict[str, list[str]] = handler.query
        self.body: Any = handler.body
        self.authorization: str | None = handler.headers.get("Authorization")

    # -- 便捷方法
    def q(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def require_body(self) -> dict[str, Any]:
        if not isinstance(self.body, dict):
            raise DomainError(400, "bad_request", "请求体必须是 JSON 对象")
        return self.body

    def user(self) -> sqlite3.Row:
        return domain.authenticate(self.store, self.settings, self.authorization)


# ---------------------------------------------------------------- 各端点实现

def h_healthz(ctx: RequestContext) -> tuple[int, Any]:
    counts = ctx.store.counts()
    return 200, {"ok": True, "periods": counts["snapshot"], "accounts": counts["account"],
                 "items": counts["snapshot_item"], "users": counts["user"],
                 # 公开的注册开关状态：登录页据此显示「首次使用：创建账号」
                 "registration_open": domain.registration_open(ctx.store, ctx.settings),
                 # 密钥指纹与库中记录不一致（.env 被重建/丢失）→ 已有账号将无法登录
                 "secret_mismatch": domain.secret_mismatch(ctx.store, ctx.settings)}


def h_register(ctx: RequestContext) -> tuple[int, Any]:
    body = ctx.require_body()
    result = domain.register(ctx.store, ctx.settings, body.get("username") or "",
                             body.get("password") or "")
    return 201, {"ok": True, "username": result["username"],
                 "registration_open": domain.registration_open(ctx.store, ctx.settings)}


def h_login(ctx: RequestContext) -> tuple[int, Any]:
    body = ctx.require_body()
    return 200, domain.login(ctx.store, ctx.settings, body.get("username") or "",
                             body.get("password") or "")


def h_logout(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.logout(ctx.store, ctx.authorization)


def h_me(ctx: RequestContext) -> tuple[int, Any]:
    user = ctx.user()
    return 200, {"username": user["username"], "created_at": user["created_at"]}


def h_accounts_list(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, {"accounts": domain.account_views(ctx.store)}


def h_accounts_create(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    body = ctx.require_body()
    name = (body.get("name") or "").strip()
    if not name:
        raise DomainError(400, "bad_request", "缺少 name")
    kind = str(body.get("kind") or "asset")
    if kind not in {"asset", "liability"}:
        raise DomainError(400, "bad_request", f"kind 只能是 asset/liability：{kind}")
    fields = {
        "category": body.get("category"), "subclass": body.get("subclass"),
        "currency": body.get("currency") or "CNY",
        "is_liquid": 1 if body.get("is_liquid") else 0,
        "is_counted": 0 if body.get("is_counted") in (0, False) else 1,
        "sort": int(body.get("sort") or 0),
        "active_from": body.get("active_from"), "active_to": body.get("active_to"),
        "note": body.get("note"),
    }
    try:
        account_id = ctx.store.create_account(name, kind, **fields)
    except ConflictError as exc:
        raise DomainError(409, "conflict", str(exc)) from exc
    for alias in body.get("aliases") or []:
        try:
            ctx.store.add_alias(account_id, str(alias))
        except ConflictError as exc:
            raise DomainError(409, "conflict", str(exc)) from exc
    row = ctx.store.get_account(account_id)
    return 201, {"id": account_id, "name": row["name"], "kind": row["kind"]}


def h_accounts_patch(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    account_id = int(ctx.params["id"])
    if ctx.store.get_account(account_id) is None:
        raise DomainError(404, "not_found", f"账户不存在：{account_id}")
    body = ctx.require_body()
    fields = dict(body)
    if "kind" in fields and fields["kind"] not in {"asset", "liability"}:
        raise DomainError(400, "bad_request", "kind 只能是 asset/liability")
    for key in ("is_liquid", "is_counted"):
        if key in fields:
            fields[key] = 1 if fields[key] in (1, True, "1") else 0
    try:
        ctx.store.update_account(account_id, fields)
    except ConflictError as exc:
        raise DomainError(409, "conflict", str(exc)) from exc
    row = ctx.store.get_account(account_id)
    return 200, {"id": account_id, "name": row["name"], "kind": row["kind"],
                 "is_counted": int(row["is_counted"] or 0), "category": row["category"],
                 "subclass": row["subclass"], "note": row["note"]}


def h_alias_add(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    account_id = int(ctx.params["id"])
    if ctx.store.get_account(account_id) is None:
        raise DomainError(404, "not_found", f"账户不存在：{account_id}")
    body = ctx.require_body()
    alias = (body.get("alias") or "").strip()
    if not alias:
        raise DomainError(400, "bad_request", "缺少 alias")
    try:
        ctx.store.add_alias(account_id, alias)
    except ConflictError as exc:
        raise DomainError(409, "conflict", str(exc)) from exc
    return 201, {"ok": True, "account_id": account_id, "alias": alias}


def h_account_series(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.account_series(ctx.store, int(ctx.params["id"]))


def h_journal(ctx: RequestContext) -> tuple[int, Any]:
    """GET /api/journal?scope=all|journal|account&account_id=&q=&limit="""
    ctx.user()
    scope = (ctx.q("scope") or "all").strip()
    if scope not in ("all", "journal", "account"):
        raise DomainError(400, "bad_request", "scope 只能是 all / journal / account")
    account_raw = (ctx.q("account_id") or "").strip()
    if account_raw and not account_raw.isdigit():
        raise DomainError(400, "bad_request", "account_id 必须是数字")
    limit = 300
    limit_raw = (ctx.q("limit") or "").strip()
    if limit_raw:
        try:
            limit = max(1, min(2000, int(limit_raw)))
        except ValueError as exc:
            raise DomainError(400, "bad_request", "limit 必须是整数") from exc
    return 200, domain.journal(
        ctx.store,
        scope=scope,
        account_id=int(account_raw) if account_raw else None,
        keyword=ctx.q("q") or "",
        limit=limit,
    )


def h_snapshots_list(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, {"snapshots": domain.snapshot_list(ctx.store)}


def h_snapshot_get(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.snapshot_detail(ctx.store, ctx.params["date"])


def h_carry_forward(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    body = ctx.body if isinstance(ctx.body, dict) else {}
    return 200, domain.carry_forward(ctx.store, body.get("from") or ctx.q("from"))


def h_snapshot_create(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.save_snapshot(ctx.store, ctx.require_body())


def h_snapshot_put(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.save_snapshot(ctx.store, ctx.require_body(), date_from_path=ctx.params["date"])


def h_snapshot_delete(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.delete_snapshot(ctx.store, ctx.params["date"])


def h_snapshot_diff(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.snapshot_diff(ctx.store, ctx.params["date"], ctx.q("against"))


def h_fx(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    refresh = str(ctx.q("refresh") or "").lower() in {"1", "true", "yes"}
    return 200, domain.fx_lookup(ctx.store, ctx.q("date"), refresh=refresh)


def h_metrics_networth(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.networth_metrics(ctx.store)


def h_metrics_structure(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.structure_metrics(ctx.store, ctx.q("date"))


def h_metrics_reconcile(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.reconcile_metrics(ctx.store, ctx.settings)


def h_export(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.export_payload(ctx.store)


def h_import(ctx: RequestContext) -> tuple[int, Any]:
    ctx.user()
    return 200, domain.import_payload(ctx.store, ctx.require_body())


# 路由表：先匹配 path，再匹配 method（顺序敏感：具体路径在前）
ROUTES: list[tuple[str, re.Pattern[str], Callable[[RequestContext], tuple[int, Any]], bool]] = [
    ("GET", re.compile(r"^/api/healthz/?$"), h_healthz, False),
    ("POST", re.compile(r"^/api/auth/register/?$"), h_register, False),
    ("POST", re.compile(r"^/api/auth/login/?$"), h_login, False),
    ("POST", re.compile(r"^/api/auth/logout/?$"), h_logout, True),
    ("GET", re.compile(r"^/api/me/?$"), h_me, True),
    ("GET", re.compile(r"^/api/fx/?$"), h_fx, True),
    ("GET", re.compile(r"^/api/accounts/?$"), h_accounts_list, True),
    ("POST", re.compile(r"^/api/accounts/?$"), h_accounts_create, True),
    ("PATCH", re.compile(r"^/api/accounts/(?P<id>\d+)/?$"), h_accounts_patch, True),
    ("POST", re.compile(r"^/api/accounts/(?P<id>\d+)/aliases/?$"), h_alias_add, True),
    ("GET", re.compile(r"^/api/accounts/(?P<id>\d+)/series/?$"), h_account_series, True),
    ("GET", re.compile(r"^/api/snapshots/?$"), h_snapshots_list, True),
    ("POST", re.compile(r"^/api/snapshots/carry-forward/?$"), h_carry_forward, True),
    ("POST", re.compile(r"^/api/snapshots/?$"), h_snapshot_create, True),
    ("GET", re.compile(r"^/api/snapshots/(?P<date>[\d\-/]+)/diff/?$"), h_snapshot_diff, True),
    ("GET", re.compile(r"^/api/snapshots/(?P<date>[\d\-/]+)/?$"), h_snapshot_get, True),
    ("PUT", re.compile(r"^/api/snapshots/(?P<date>[\d\-/]+)/?$"), h_snapshot_put, True),
    ("DELETE", re.compile(r"^/api/snapshots/(?P<date>[\d\-/]+)/?$"), h_snapshot_delete, True),
    ("GET", re.compile(r"^/api/metrics/networth/?$"), h_metrics_networth, True),
    ("GET", re.compile(r"^/api/journal/?$"), h_journal, True),
    ("GET", re.compile(r"^/api/metrics/structure/?$"), h_metrics_structure, True),
    ("GET", re.compile(r"^/api/metrics/reconcile/?$"), h_metrics_reconcile, True),
    ("GET", re.compile(r"^/api/export/?$"), h_export, True),
    ("POST", re.compile(r"^/api/import/?$"), h_import, True),
]


class ApiHandler(BaseHTTPRequestHandler):
    """单个请求的处理：路由 -> 鉴权 -> 业务 -> JSON 响应。"""

    protocol_version = "HTTP/1.1"
    server_version = "networth/1.0"

    # -- 由 build_server 注入的属性
    settings: Settings
    store: Store
    verbose: bool = False

    # ------------------------------------------------------------ 入口

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        if self.verbose:
            sys.stderr.write("[networth] %s - %s\n" % (self.address_string(), format % args))

    # ------------------------------------------------------------ 主流程

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        self.query = parse_qs(parsed.query)
        self.body = None

        try:
            if not path.startswith("/api"):
                self._serve_static(method, path)
                return

            self.body = self._read_body()
            matched_path = False
            for route_method, pattern, handler, need_auth in ROUTES:
                match = pattern.match(path)
                if not match:
                    continue
                matched_path = True
                if route_method != method:
                    continue
                ctx = RequestContext(self, {k: v for k, v in match.groupdict().items()})
                if need_auth:
                    ctx.user()  # 统一 401 语义
                status, payload = handler(ctx)
                self._send_json(status, payload)
                return
            if matched_path:
                self._error(405, "method_not_allowed", f"方法不被允许：{method} {path}")
            else:
                self._error(404, "not_found", f"未知接口：{path}")
        except DomainError as exc:
            self._error(exc.status, exc.code, exc.message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "bad_request", "请求体不是合法 JSON")
        except Exception:  # noqa: BLE001 - 兜底 500，同时保留栈便于排查
            traceback.print_exc()
            self._error(500, "internal_error", "服务器内部错误")

    def _read_body(self) -> Any:
        length_raw = self.headers.get("Content-Length")
        if not length_raw:
            return None
        try:
            length = int(length_raw)
        except ValueError as exc:
            raise DomainError(400, "bad_request", "Content-Length 非法") from exc
        if length <= 0:
            return None
        if length > MAX_BODY:
            raise DomainError(413, "payload_too_large", "请求体过大")
        raw = self.rfile.read(length)
        if not raw.strip():
            return None
        return json.loads(raw.decode("utf-8"))

    # ------------------------------------------------------------ 响应

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _serve_static(self, method: str, path: str) -> None:
        """托管前端构建产物（若存在）；SPA 路由回退 index.html。"""
        if method not in ("GET", "HEAD"):
            self._error(405, "method_not_allowed", "静态资源仅支持 GET")
            return
        dist: Path = self.settings.web_dist
        index = dist / "index.html"
        if not index.exists():
            self._error(404, "not_found", "前端构建产物不存在（请先构建 frontend/dist）")
            return
        candidate = (dist / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            self._error(403, "forbidden", "非法路径")
            return
        if candidate.is_dir() or not candidate.exists():
            candidate = index
        data = candidate.read_bytes()
        ctype = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if candidate.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method == "GET":
            self.wfile.write(data)


class NetworthServer(ThreadingHTTPServer):
    """带 store/settings 的服务器；daemon_threads 保证进程能干净退出。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler_cls: type[BaseHTTPRequestHandler],
                 settings: Settings, store: Store, verbose: bool = False) -> None:
        self.settings = settings
        self.store = store
        self.verbose = verbose
        super().__init__(address, handler_cls, bind_and_activate=True)


def build_server(settings: Settings, store: Store, host: str | None = None,
                 port: int | None = None, verbose: bool = False) -> NetworthServer:
    """构造（未启动的）HTTP 服务器；host/port 默认取配置（端口 0 表示随机可用端口）。

    每个服务器持有自己的 handler 子类，避免多个实例（例如测试里并行起两个库）互相串配置。
    """
    address = (host or settings.bind, settings.port if port is None else port)

    class BoundHandler(ApiHandler):
        pass

    BoundHandler.settings = settings
    BoundHandler.store = store
    BoundHandler.verbose = verbose
    return NetworthServer(address, BoundHandler, settings, store, verbose=verbose)
