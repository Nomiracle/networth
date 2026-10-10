"""净值管家 MCP 工具定义与后端调用（纯标准库，HTTP 客户端，不直连数据库）。"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------- 环境与 backend 调用

def _api_base() -> str:
    """后端 base URL，可用 NETWORTH_API_BASE 覆盖，默认 127.0.0.1:8972。"""
    raw = os.environ.get("NETWORTH_API_BASE", "").strip()
    if raw:
        return raw.rstrip("/")
    return "http://127.0.0.1:8972"

def _mcp_token() -> str | None:
    tok = os.environ.get("NETWORTH_MCP_TOKEN", "").strip()
    return tok if tok else None

def _backend_request(method: str, path: str, body: Any | None = None, query: dict[str, str] | None = None) -> tuple[int, Any]:
    """向后端发 HTTP，返回 (status, json_payload)。仅标准库，不直连数据库。"""
    base = _api_base()
    url = base + path
    if query:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
    data: bytes | None = None
    headers: dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = _mcp_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            status = resp.status
            text = raw.decode("utf-8") if raw else ""
            payload = json.loads(text) if text.strip() else None
            return status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        text = raw.decode("utf-8") if raw else ""
        try:
            payload = json.loads(text) if text.strip() else {"error": {"code": "http_error", "message": text}}
        except json.JSONDecodeError:
            payload = {"error": {"code": "http_error", "message": text}}
        # 保留后端 error.code 供上层透传
        return exc.code, payload
    except Exception as exc:  # noqa: BLE001
        return 502, {"error": {"code": "backend_unreachable", "message": f"后端不可达: {exc}"}}

def _tool_error_text(status: int, payload: Any) -> str:
    """把后端错误转成 tools/call isError 文本（含 HTTP 状态与 error.code）。"""
    if isinstance(payload, dict) and "error" in payload:
        err = payload["error"]
        code = err.get("code", "unknown")
        msg = err.get("message", "")
        return f"后端错误 HTTP {status} code={code}: {msg}"
    return f"后端错误 HTTP {status}: {json.dumps(payload, ensure_ascii=False)}"

def _format_amount_note(value: Any, currency: str) -> str:
    """金额标明币种，供结果文本使用。"""
    if value is None:
        return f"未记录 ({currency})"
    return f"{value} {currency}"

# ---------------------------------------------------------------- 工具定义

# 只读工具（默认全部可用）
READONLY_TOOLS: list[dict[str, Any]] = [
    {
        "name": "networth_health",
        "description": "检查后端服务健康状态（GET /api/healthz，免鉴权透传）",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_accounts",
        "description": "列出全部账户台账（资产/负债、币种、别名、最近金额 CNY）",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_account_series",
        "description": "查询单个账户的历史时序（余额走势，金额单位 CNY）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "integer", "description": "账户 ID"},
            },
            "required": ["account_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_snapshots",
        "description": "列出全部快照期次（净值/资产/负债，金额单位 CNY）",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_snapshot",
        "description": "查询单期快照明细（含各账户金额 CNY/USD 与地点信息，未观测字段保持键缺失）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "快照日期 YYYY-MM-DD"},
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "carry_forward",
        "description": "基于上一期结转生成新期次草稿（不落库，含地点默认值 location_default）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from": {"type": "string", "description": "源快照日期 YYYY-MM-DD，未传则取最新一期"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "snapshot_diff",
        "description": "对比两期快照的逐账户变动（环比，金额单位 CNY）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "目标日期 YYYY-MM-DD"},
                "against": {"type": "string", "description": "对比基准日期 YYYY-MM-DD，未传则取上一期"},
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
    {
        "name": "networth_metrics",
        "description": "净值曲线与关键指标（当前净值 CNY、涨幅、美元占比等）",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "structure_metrics",
        "description": "资产结构与负债结构（按类别分组、占比，金额单位 CNY）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "快照日期 YYYY-MM-DD，未传则取最新"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "journal",
        "description": "查询投资日志与账户备注（支持按期、账户、关键词过滤）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["all", "journal", "account"], "description": "范围"},
                "account_id": {"type": "integer", "description": "账户 ID"},
                "q": {"type": "string", "description": "关键词"},
                "limit": {"type": "integer", "description": "返回条数上限", "minimum": 1, "maximum": 2000},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "fx_rate",
        "description": "查询指定日期的 USD→CNY 汇率（ECB 中间价，需登录）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "日期 YYYY-MM-DD，未传则取今天"},
                "refresh": {"type": "boolean", "description": "是否强制刷新缓存"},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "reverse_geocode",
        "description": "逆地理编码：将经纬度解析为地点文本（GET /api/location/reverse）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "纬度"},
                "lon": {"type": "number", "description": "经度"},
                "refresh": {"type": "boolean", "description": "是否强制刷新"},
            },
            "required": ["lat", "lon"],
            "additionalProperties": False,
        },
    },
    {
        "name": "location_metrics",
        "description": "地点指标：按地点聚合的快照统计（GET /api/metrics/locations）",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

WRITE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "save_snapshot",
        "description": "保存（新建或覆盖）一期快照明细（写后读验证，金额需标明 CNY/USD）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "快照日期 YYYY-MM-DD"},
                "items": {
                    "type": "array",
                    "description": "明细行",
                    "items": {
                        "type": "object",
                        "properties": {
                            "account_id": {"type": "integer", "description": "账户 ID"},
                            "amount_cny": {"type": "number", "description": "人民币金额 CNY"},
                            "amount_usd": {"type": "number", "description": "美元金额 USD"},
                            "fx_rate": {"type": "number", "description": "汇率"},
                            "note": {"type": "string", "description": "备注"},
                            "auto_filled": {"type": "integer", "enum": [0, 1], "description": "是否自动结转"},
                        },
                        "required": ["account_id"],
                        "additionalProperties": False,
                    },
                },
                "default_fx_rate": {"type": "number", "description": "期默认汇率"},
                "market_note": {"type": "string", "description": "市场备注"},
                "status": {"type": "string", "enum": ["final", "draft"], "description": "状态"},
            },
            "required": ["date", "items"],
            "additionalProperties": False,
        },
    },
    {
        "name": "patch_account",
        "description": "更新账户元数据（名称、分类、币种等，写后读验证）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "账户 ID"},
                "name": {"type": "string", "description": "账户名称"},
                "category": {"type": "string", "description": "大类"},
                "subclass": {"type": "string", "description": "小类"},
                "kind": {"type": "string", "enum": ["asset", "liability"], "description": "资产/负债"},
                "currency": {"type": "string", "enum": ["CNY", "USD"], "description": "币种"},
                "is_liquid": {"type": "integer", "enum": [0, 1], "description": "是否流动"},
                "is_counted": {"type": "integer", "enum": [0, 1], "description": "是否计入统计"},
                "sort": {"type": "integer", "description": "排序"},
                "active_from": {"type": "string", "description": "生效起"},
                "active_to": {"type": "string", "description": "生效止"},
                "note": {"type": "string", "description": "备注"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_location",
        "description": "设置指定日期快照的地点信息（写后读验证，未观测字段保持键缺失加原因码）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "快照日期 YYYY-MM-DD"},
                "text": {"type": "string", "description": "地点文本"},
                "source": {"type": "string", "enum": ["manual", "carried", "device"], "description": "来源（parsed_note 只由后端回填 CLI 写）"},
                "observed_at": {"type": "string", "description": "观测时间 ISO8601"},
                "lat": {"type": "number", "description": "纬度"},
                "lon": {"type": "number", "description": "经度"},
                "accuracy_m": {"type": "number", "description": "精度（米）"},
                "clear": {"type": "boolean", "description": "true 表示清空该期地点（回到未观测）"},
            },
            "required": ["date"],
            "additionalProperties": False,
        },
    },
]

def all_tools() -> list[dict[str, Any]]:
    """根据环境变量决定可见工具列表。"""
    allow = os.environ.get("NETWORTH_MCP_ALLOW_WRITE", "").strip() == "1"
    if allow:
        return READONLY_TOOLS + WRITE_TOOLS
    return list(READONLY_TOOLS)

def get_tool(name: str) -> dict[str, Any] | None:
    for t in READONLY_TOOLS + WRITE_TOOLS:
        if t["name"] == name:
            return t
    return None

# ---------------------------------------------------------------- 参数校验

def _validate_params(schema: dict[str, Any], params: Any) -> str | None:
    """轻量 JSON Schema 校验，返回错误信息或 None。"""
    if not isinstance(params, dict):
        return "params 必须是对象"
    props = schema.get("properties", {})
    required = schema.get("required", [])
    for key in required:
        if key not in params:
            return f"缺少必填参数: {key}"
    for key, value in params.items():
        if key not in props:
            if not schema.get("additionalProperties", True):
                return f"未知参数: {key}"
            continue
        spec = props[key]
        expected = spec.get("type")
        if expected == "string" and not isinstance(value, str):
            return f"参数 {key} 类型错误：期望 string，实际 {type(value).__name__}"
        if expected == "integer" and not isinstance(value, int):
            # JSON 中数字可能为 float 形式的整数
            if isinstance(value, float) and value.is_integer():
                continue
            return f"参数 {key} 类型错误：期望 integer，实际 {type(value).__name__}"
        if expected == "number" and not isinstance(value, (int, float)):
            return f"参数 {key} 类型错误：期望 number，实际 {type(value).__name__}"
        if expected == "boolean" and not isinstance(value, bool):
            return f"参数 {key} 类型错误：期望 boolean，实际 {type(value).__name__}"
        if expected == "array" and not isinstance(value, list):
            return f"参数 {key} 类型错误：期望 array，实际 {type(value).__name__}"
        if "enum" in spec and value not in spec["enum"]:
            return f"参数 {key} 取值非法：{value} 不在 {spec['enum']} 之中"
        if expected == "integer" and "minimum" in spec and isinstance(value, int) and value < spec["minimum"]:
            return f"参数 {key} 小于最小值 {spec['minimum']}"
        if expected == "integer" and "maximum" in spec and isinstance(value, int) and value > spec["maximum"]:
            return f"参数 {key} 大于最大值 {spec['maximum']}"
    return None

# ---------------------------------------------------------------- 业务调用

def _call_readonly(name: str, args: dict[str, Any]) -> tuple[Any, bool, str | None]:
    """只读工具直接透传后端，返回 (payload, isError, errText)。"""
    if name == "networth_health":
        status, payload = _backend_request("GET", "/api/healthz")
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "list_accounts":
        status, payload = _backend_request("GET", "/api/accounts")
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        # 金额已含币种字段，透传即可
        return payload, False, None
    if name == "get_account_series":
        aid = args["account_id"]
        status, payload = _backend_request("GET", f"/api/accounts/{aid}/series")
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "list_snapshots":
        status, payload = _backend_request("GET", "/api/snapshots")
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "get_snapshot":
        date = args["date"]
        status, payload = _backend_request("GET", f"/api/snapshots/{date}")
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        # 未观测字段保持键缺失，后端已按契约处理
        return payload, False, None
    if name == "carry_forward":
        body: dict[str, Any] = {}
        if "from" in args:
            body["from"] = args["from"]
        status, payload = _backend_request("POST", "/api/snapshots/carry-forward", body if body else {})
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "snapshot_diff":
        date = args["date"]
        query = {}
        if "against" in args and args["against"]:
            query["against"] = args["against"]
        status, payload = _backend_request("GET", f"/api/snapshots/{date}/diff", query=query if query else None)
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "networth_metrics":
        status, payload = _backend_request("GET", "/api/metrics/networth")
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "structure_metrics":
        query = {}
        if "date" in args and args["date"]:
            query["date"] = args["date"]
        status, payload = _backend_request("GET", "/api/metrics/structure", query=query if query else None)
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "journal":
        query: dict[str, str] = {}
        if "scope" in args and args["scope"]:
            query["scope"] = str(args["scope"])
        if "account_id" in args and args["account_id"] is not None:
            query["account_id"] = str(args["account_id"])
        if "q" in args and args["q"]:
            query["q"] = str(args["q"])
        if "limit" in args and args["limit"] is not None:
            query["limit"] = str(args["limit"])
        status, payload = _backend_request("GET", "/api/journal", query=query if query else None)
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "fx_rate":
        query = {}
        if "date" in args and args["date"]:
            query["date"] = str(args["date"])
        if args.get("refresh"):
            query["refresh"] = "1"
        status, payload = _backend_request("GET", "/api/fx", query=query if query else None)
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "reverse_geocode":
        query = {"lat": str(args["lat"]), "lon": str(args["lon"])}
        if args.get("refresh"):
            query["refresh"] = "1"
        status, payload = _backend_request("GET", "/api/location/reverse", query=query)
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    if name == "location_metrics":
        status, payload = _backend_request("GET", "/api/metrics/locations")
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        return payload, False, None
    return None, True, f"未知只读工具: {name}"

def _call_write(name: str, args: dict[str, Any]) -> tuple[Any, bool, str | None]:
    """写工具：先写、再 GET 回读、把回读值放进返回内容。"""
    if name == "save_snapshot":
        date = args["date"]
        items = args["items"]
        body: dict[str, Any] = {"date": date, "items": items}
        if "default_fx_rate" in args:
            body["default_fx_rate"] = args["default_fx_rate"]
        if "market_note" in args:
            body["market_note"] = args["market_note"]
        if "status" in args:
            body["status"] = args["status"]
        # 优先 PUT /api/snapshots/{date}（幂等），失败则 POST /api/snapshots
        status, payload = _backend_request("PUT", f"/api/snapshots/{date}", body)
        if status >= 400:
            # 若后端不支持 PUT，回退到 POST
            if status == 404 or status == 405:
                status2, payload2 = _backend_request("POST", "/api/snapshots", body)
                if status2 >= 400:
                    return None, True, _tool_error_text(status2, payload2)
                status, payload = status2, payload2
            else:
                return None, True, _tool_error_text(status, payload)
        # 写后读验证：GET 回读
        r_status, r_payload = _backend_request("GET", f"/api/snapshots/{date}")
        if r_status != 200:
            return None, True, f"写后读验证失败 GET /api/snapshots/{date} HTTP {r_status}: {json.dumps(r_payload, ensure_ascii=False)}"
        return {"write_result": payload, "readback": r_payload, "verified": True, "currency": "CNY"}, False, None

    if name == "patch_account":
        aid = args["id"]
        # 构造 PATCH body，排除 id 本身
        patch_body: dict[str, Any] = {k: v for k, v in args.items() if k != "id"}
        if not patch_body:
            return None, True, "至少提供一个待更新字段"
        status, payload = _backend_request("PATCH", f"/api/accounts/{aid}", patch_body)
        if status != 200:
            return None, True, _tool_error_text(status, payload)
        # 写后读验证：GET 账户列表并定位该账户，或 GET series
        r_status, r_payload = _backend_request("GET", "/api/accounts")
        readback = None
        if r_status == 200 and isinstance(r_payload, dict) and "accounts" in r_payload:
            for acc in r_payload["accounts"]:
                if acc.get("id") == aid:
                    readback = acc
                    break
        if readback is None:
            # 回退到 series 读取
            s_status, s_payload = _backend_request("GET", f"/api/accounts/{aid}/series")
            if s_status == 200:
                readback = s_payload
            else:
                readback = r_payload
        # 币种标明
        return {"write_result": payload, "readback": readback, "verified": True, "currency": "CNY"}, False, None

    if name == "set_location":
        date = args["date"]
        if args.get("clear") is True:
            # 显式清空：location=null，后端把六列一起置 NULL（回到「未观测」）
            loc: Any = None
        else:
            loc = {}
            for key in ("text", "source", "observed_at", "lat", "lon", "accuracy_m"):
                if key in args and args[key] is not None:
                    loc[key] = args[key]
            if not loc:
                return None, True, "至少提供 text（或用 clear=true 清空地点）"
        # 只走地点专用窄接口。绝不退化到整期 PUT /api/snapshots/{date}：那是 delete+insert
        # items 的全量写，不带 items 就等于清空该期明细（后端现在也会以 400
        # partial_update_unsupported 拒绝，但 MCP 侧不该发出这种请求）。
        head = f"/api/snapshots/{date}/location"
        w_status, w_payload = _backend_request("PUT", head, {"location": loc})
        if w_status in (404, 405):
            # 兼容只提供 POST 的后端（同一窄接口的另一种方法）
            w_status, w_payload = _backend_request("POST", head, {"location": loc})
        if w_status >= 400:
            return None, True, (
                _tool_error_text(w_status, w_payload)
                + f"（地点专用接口 {head} 不可用；不会退化到整期 PUT，以免清空该期明细）"
            )
        # 写后读验证
        r_status, r_payload = _backend_request("GET", f"/api/snapshots/{date}")
        if r_status != 200:
            return None, True, f"写后读验证失败 GET /api/snapshots/{date} HTTP {r_status}: {json.dumps(r_payload, ensure_ascii=False)}"
        verified = r_payload.get("snapshot", r_payload) if isinstance(r_payload, dict) else r_payload
        return {"write_result": w_payload, "readback": verified, "verified": True,
                "location": (verified.get("location") if isinstance(verified, dict) else None),
                "location_absent_reason": (verified.get("location_absent_reason") if isinstance(verified, dict) else None)}, False, None

    return None, True, f"未知写工具: {name}"

def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """分发工具调用，返回 MCP tools/call 结果（content + isError）。"""
    tool = get_tool(name)
    if tool is None:
        # 防护：工具名不在白名单（包括写工具未开放时）
        return {
            "content": [{"type": "text", "text": f"未知工具: {name}"}],
            "isError": True,
        }
    # 校验是否允许写工具
    allow_write = os.environ.get("NETWORTH_MCP_ALLOW_WRITE", "").strip() == "1"
    if tool in WRITE_TOOLS and not allow_write:
        return {
            "content": [{"type": "text", "text": f"写工具未启用（需 NETWORTH_MCP_ALLOW_WRITE=1）: {name}"}],
            "isError": True,
        }
    # 参数校验
    schema = tool["inputSchema"]
    err = _validate_params(schema, args)
    if err:
        # 参数错误按 MCP 要求由上层转成 -32602，这里返回标记供 server 层判断
        return {"_validation_error": err}  # type: ignore

    if tool in READONLY_TOOLS:
        payload, is_err, err_text = _call_readonly(name, args)
    else:
        payload, is_err, err_text = _call_write(name, args)

    if is_err:
        return {
            "content": [{"type": "text", "text": err_text or "未知错误"}],
            "isError": True,
        }
    # 成功：把 payload 转文本，保留结构化信息
    text = json.dumps(payload, ensure_ascii=False, indent=2) if payload is not None else "null"
    # 追加币种说明（若未在 payload 中）
    return {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }
