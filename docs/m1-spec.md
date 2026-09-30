# networth M1 实施契约（后端 / 前端共同依据）

项目：`<PROJECT_ROOT>`　服务名 `networth`（显示名「净值管家」）
数据源：你自己的资产负债表 xlsx → M0 解析产物 `data/out/*.json`（见 `backend/app/importer.py`）

## 0. 硬约束（违反即返工）

- **后端禁止 pip install / 第三方依赖**。只用 Python 3.13 标准库
  （`http.server` `socketserver` `sqlite3` `json` `hashlib` `hmac` `secrets` `urllib.parse`
  `threading` `email.utils` `unittest` `decimal`）。运行解释器 `/usr/bin/python3`。
- **前端禁止 npm install**。依赖使用一份本地已安装的 `node_modules`
  （vue 3.5 / vue-router 4.x / pinia 2.x / element-plus 2.x / echarts 5.x /
  axios 1.x / vite 5.x / typescript 5.x / vue-tsc 2.x / @vitejs/plugin-vue 5.x）。
  做法：`ln -s <NODE_MODULES_DIR> frontend/node_modules`，或直接 `npm install`。
- 不写 secret 到仓库；DB 路径与 token 密钥从环境变量读取，未设置时用本地默认值。
- 不改动项目目录之外的任何文件。

## 1. 计算规则（已用 62 期历史验证）

- **净资产 = Σ 该期所有明细行的有符号 `amount_cny`**（负债行通常为负；负债块里允许出现正号行，
  例如某期「私人借款 +4000」、另一期「某信用卡 +3135」这类原表写法）。
  该口径与服务端的逐期重算逐期零差异，是唯一正确的复现口径。
- `total_assets = Σ amount`（kind=asset）
- `total_liabilities = Σ amount`（kind=liability，通常为负数）
- `networth = total_assets + total_liabilities`
- **服务端永远重算**：小计/资产总计/净资产行一律不导入、不接受前端传入。
- 符号语义由 `kind` 决定，不信任原表正负号。

## 2. SQLite 模型（`backend` 启动时自动建表 + 幂等迁移）

```sql
CREATE TABLE user (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE token (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
  token_digest TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  revoked_at TEXT);
CREATE TABLE account (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
  category TEXT, subclass TEXT, kind TEXT NOT NULL CHECK(kind IN ('asset','liability')),
  currency TEXT DEFAULT 'CNY', is_liquid INTEGER DEFAULT 0, is_counted INTEGER DEFAULT 1,
  sort INTEGER DEFAULT 0, active_from TEXT, active_to TEXT, note TEXT);
CREATE TABLE account_alias (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL,
  alias TEXT UNIQUE NOT NULL);
CREATE TABLE snapshot (id INTEGER PRIMARY KEY, snapshot_date TEXT UNIQUE NOT NULL,
  status TEXT DEFAULT 'final', default_fx_rate REAL, market_note TEXT,
  data_quality_flags TEXT, source TEXT DEFAULT 'manual', created_at TEXT, updated_at TEXT);
CREATE TABLE snapshot_item (id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL, amount_cny REAL NOT NULL, amount_usd REAL, fx_rate REAL,
  note TEXT, auto_filled INTEGER DEFAULT 0, flags TEXT);
CREATE TABLE event (id INTEGER PRIMARY KEY, event_date TEXT NOT NULL, kind TEXT NOT NULL,
  amount_cny REAL NOT NULL, note TEXT, created_at TEXT);
CREATE UNIQUE INDEX snapshot_item_uq ON snapshot_item(snapshot_id, account_id);
```

## 3. API 契约（前缀 `/api`，JSON；除 auth 与 /healthz 外都要 `Authorization: Bearer <token>`）

- `GET  /api/healthz` → `{"ok":true,"periods":N,"accounts":M}`
- `POST /api/auth/register` `{username,password}` → 仅当库中无用户或 `ALLOW_REGISTRATION=1`；
  首账号注册成功后，若无该环境变量则自动关闭注册
- `POST /api/auth/login` `{username,password}` → `{token, expires_at, username}`（token 只在此时返回明文）
- `POST /api/auth/logout`、`GET /api/me` → `{username, created_at}`
- `GET  /api/accounts` → `{accounts:[{id,name,category,subclass,kind,aliases:[],last_amount,last_date,periods,unchanged_tail,is_counted}]}`
- `POST /api/accounts`、`PATCH /api/accounts/{id}`、`POST /api/accounts/{id}/aliases {alias}`
- `GET  /api/accounts/{id}/series` → `{account, points:[{date,amount,delta}]}`
- `GET  /api/snapshots` → `{snapshots:[{date,status,networth,total_assets,total_liabilities,item_count,market_note,data_quality_flags}]}`
- `GET  /api/snapshots/{date}` → `{snapshot, items:[{account_id,account,kind,category,subclass,amount_cny,amount_usd,fx_rate,note,auto_filled}]}`
- `POST /api/snapshots/carry-forward` `{from?}` → `{date_default, fx_rate_default, items:[…]}`（不落库）
- `POST /api/snapshots` / `PUT /api/snapshots/{date}` `{date,default_fx_rate,market_note,items:[{account_id,amount_cny,amount_usd?,fx_rate?,note?,auto_filled?}],status?}`
  → upsert（按 date，事务内 delete+insert items）
- `DELETE /api/snapshots/{date}`
- `GET  /api/snapshots/{date}/diff?against=YYYY-MM-DD` → `{rows:[{account,before,after,delta,pct}],totals:{before,after,delta}}`
- `GET  /api/metrics/networth` → `{points:[{date,networth,total_assets,total_liabilities,delta,growth_pct}],kpi:{current,change_pct,ytd_pct,usd_share,debt_ratio,periods}}`
- `GET  /api/metrics/structure?date=` → `{date,groups:[{name,amount,share}],liabilities:[{name,amount,share}]}`
- `GET  /api/metrics/reconcile` → `{periods:[{date,imported,computed,diff,flags:[]}]}`（来自 M0 对账数据）
- `GET  /api/export` → `{version:1,exported_at,accounts:[…],snapshots:[{…,items:[…]}],events:[…]}`
- `POST /api/import`（导出同构；幂等 upsert）
- 错误统一 `{"error":{"code":…,"message":…}}`，HTTP 400/401/403/404/409/500

## 4. 历史导入（`backend` 自带 CLI）

`python3 -m app.importer --source ../data/out --db <path>`：

1. `accounts.json` → 建 `account` + `account_alias`（`理财/基金（模板行）` 导入但 `is_counted=0`）
2. `snapshots.json` → 每期一个 `snapshot`（`source='excel'`），明细行按 `snapshot_item_uq` 幂等 upsert；
   丢弃所有 `小计/总计/净资产` 行；`data_quality_flags` 记录 `extra_table` / 重复计入
3. `reconcile.json` 落到 `data/out/reconcile-imported.json` 供 `/api/metrics/reconcile` 读取
4. 结束打印：`accounts=N snapshots=M items=K adjusted=A mismatched=0`，若任一期 diff 超容差以非零码退出

## 5. 必须通过的验收（子任务必须真跑并回贴输出）

后端：
- `python3 -m unittest discover -s tests -v` 全绿（数据由 `tools/seed_demo.py` 生成）
- 数据集锚点（生成器给出的前/中/末期 `computed`）必须精确命中
- 真实 HTTP 往返：临时用户注册 → 登录 → carry-forward → 保存新快照 → 净值点增加 →
  `GET /api/export` → 导入全新 DB → 各表计数一致 → 删除临时数据
- 导入后 `/api/metrics/networth` 的点数必须等于数据集期数，且与 `reconcile.json` 的 `computed` 全部一致

前端：
- `node .bin/vue-tsc -b`（或 `tsc --noEmit`）+ `vite build` 通过，产出 `frontend/dist/index.html` 与 `assets/*`
- `python3 -m http.server` 起 dist 后 `curl -sI` 返回 200，HTML 含应用挂载点与路由名标记
- 路由守卫：未登录访问 `/dashboard` 落到 `/login`；401 响应触发登出
- 移动端（<560px）与桌面端布局各检查一次

## 6. 环境变量

`NETWORTH_DB`（默认 `backend/data/networth.db`）、`NETWORTH_SECRET`（HMAC）、
`NETWORTH_PORT`（默认 8972）、`NETWORTH_BIND`（默认 127.0.0.1）、`ALLOW_REGISTRATION`（1/0）、
`NETWORTH_M0_OUT`（默认 `../data/out`）、`NETWORTH_WEB_DIST`（默认 `../frontend/dist`）

## 7. 目录

```
networth/
├─ backend/app/{__init__,config,store,domain,importer,api,server}.py
├─ backend/tests/test_api.py  backend/data/（DB，git 忽略）
├─ backend/run.sh            # 启动脚本（读 env，绑定 127.0.0.1:8972）
├─ backend/../tools/seed_demo.py   # 演示数据生成（仓库不含数据文件）
├─ frontend/{package.json,vite.config.ts,tsconfig.json,index.html,src/…}
├─ frontend/dist/（构建产物）
├─ scripts/xlsx_lite.py     # 标准库 xlsx 读取，供自写导入脚本复用
└─ deploy/{nginx.conf.template,networth-api.service,backup.py,deploy-web.sh}
```
