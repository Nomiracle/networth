# 净值管家 MCP 服务（networth-mcp）

纯标准库 JSON-RPC 2.0 桥接：MCP 客户端 ↔ 净值管家后端 API（`http://127.0.0.1:8972`）。

## 架构要点

- **不直连 SQLite**：`mcp/` 下无 `import sqlite3`、不打开任何 `.db`；仅作为 HTTP 客户端调用后端 API（`NETWORTH_API_BASE`，默认 `http://127.0.0.1:8972`），`Authorization: Bearer $NETWORTH_MCP_TOKEN`。
- **纯标准库**：`http.server` / `urllib` / `json` / `argparse`，零第三方依赖（规避本机 `mcp` SDK 损坏的 `pydantic_core` 问题）。
- **双传输**：STDIO 与 HTTP（`--stdio` / `--http`），HTTP 仅绑 `127.0.0.1:18084`，公网经 nginx Basic auth 暴露。

## 鉴权

- 后端调用：环境变量 `NETWORTH_MCP_TOKEN` 的 Bearer 令牌（与现有 `token` 表摘要机制一致）。
- HTTP 端：仅当配置 `NETWORTH_MCP_HTTP_TOKEN` 时要求 `Authorization: Bearer ***`；未配置则仅依赖回环绑定。

## 传输与端点

### STDIO

```bash
python3 -m mcp.server --stdio
```

每行一个 JSON-RPC 请求，逐行响应；`notifications/initialized` 为通知无响应。

### HTTP

```bash
python3 -m mcp.server --http --bind 127.0.0.1 --port 18084
# 环境变量覆盖：NETWORTH_API_BASE / NETWORTH_MCP_TOKEN / NETWORTH_MCP_HTTP_TOKEN / NETWORTH_MCP_ALLOW_WRITE
```

- `POST /mcp`：JSON-RPC（`application/json`；若 `Accept: text/event-stream` 则以 `data: {...}\n\n` 的 SSE 帧返回）
- `GET /healthz`：免鉴权，`{"ok":true,"server":"networth-mcp","readonly":<是否为只读>}`

## 协议版本协商

客户端可能发送 `2025-11-25`（Hermes 同时发 `mcp-protocol-version` 头）、`2025-06-18`、`2024-11-05`。

服务端**绝不**因版本返回 `-32000 Unsupported protocol version`；而是接受并在 `initialize` 结果里回显协商出的版本（未知版本回退到最新 `2025-11-25`）。

## 工具清单

### 只读（默认全部可见）

| 工具 | 说明 | 后端映射 |
|---|---|---|
| `networth_health` | 健康检查 | `GET /api/healthz` |
| `list_accounts` | 账户台账（CNY） | `GET /api/accounts` |
| `get_account_series` | 单账户时序 | `GET /api/accounts/{id}/series` |
| `list_snapshots` | 快照列表 | `GET /api/snapshots` |
| `get_snapshot` | 单期明细（含 location，未观测键缺失） | `GET /api/snapshots/{date}` |
| `carry_forward` | 结转草稿（含 location_default） | `POST /api/snapshots/carry-forward` |
| `snapshot_diff` | 两期环比 | `GET /api/snapshots/{date}/diff` |
| `networth_metrics` | 净值曲线（CNY） | `GET /api/metrics/networth` |
| `structure_metrics` | 结构占比（CNY） | `GET /api/metrics/structure` |
| `journal` | 投资日志 | `GET /api/journal` |
| `fx_rate` | 汇率 USD→CNY | `GET /api/fx` |
| `reverse_geocode` | 逆地理编码 | `GET /api/location/reverse` |
| `location_metrics` | 地点指标 | `GET /api/metrics/locations` |

### 写（仅 `NETWORTH_MCP_ALLOW_WRITE=1` 时出现在 `tools/list`）

| 工具 | 说明 | 写后读验证 |
|---|---|---|
| `save_snapshot` | 新建/覆盖快照 | `PUT /api/snapshots/{date}` → `GET /api/snapshots/{date}` |
| `patch_account` | 更新账户 | `PATCH /api/accounts/{id}` → `GET /api/accounts` |
| `set_location` | 设置地点 | `POST /api/snapshots/{date}/location` → `GET /api/snapshots/{date}` |

- 不提供任何 delete/清空类工具。
- 写工具**先写、再 GET 回读、把回读值放进返回内容**；`verified:true` 且金额标明币种。
- 未观测字段保持键缺失并带 `location_absent_reason`，绝不补 0。

每个工具均有中文 `description` 与严格的 JSON Schema `inputSchema`（含 `required` 与 `enum`）。

## Hermes 注册示例

```bash
# 本机回环（无 HTTP 鉴权，仅回环绑定）
/usr/local/lib/hermes-agent/venv/bin/python -c \
  "import sys; sys.path.insert(0,'/usr/local/lib/hermes-agent'); \
   from hermes_cli.mcp_config import _save_mcp_server; \
   print(_save_mcp_server('networth', {'url': 'http://127.0.0.1:18084/mcp'}))"

# 公网（Basic auth）
# hermes mcp add --url https://__DOMAIN__/mcp --auth header
```

`~/.hermes/config.yaml` 中的 `mcp_servers` 由主代理维护，本仓库不直接修改。

## 故障模式

- 后端 401/502：`tools/call` 返回 `isError:true`，`content[0].text` 含 `HTTP <状态> code=<error.code>`，绝不返回缓存或编造数据。
- 未知方法 → `-32601`；参数缺失/类型错误 → `-32602`。
- 写后读失败：返回 `isError:true`，文本含 `写后读验证失败` 与 HTTP 状态。
- HTTP 鉴权未通过 → `401 unauthorized`（仅当设置了 `NETWORTH_MCP_HTTP_TOKEN`）。
