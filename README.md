# 净值管家 networth

一个自用的「个人（家庭）资产负债表」记账服务：**每期只改动的行，系统负责结转与重算，
自动画出净值曲线、资产负债结构与逐期对账**。

- 后端：Python 3 标准库 + SQLite（**零第三方依赖**，不需要 pip install）
- 前端：Vue 3 + Vite + TypeScript + Pinia + Element Plus + ECharts
- 单用户登录（首个账号注册后自动关闭注册），数据全部保存在本地 SQLite

> **本仓库不包含任何数据。** 演示数据由 `tools/seed_demo.py` 在本地按固定随机种子生成，
> 只写入你自己的 SQLite；仓库里没有 `.json` 数据文件、没有 `.xlsx`、没有 `.db`。

## 目录

```
networth/
├─ backend/            # API 服务（标准库 + SQLite）
│  ├─ app/             #   config / store / domain / importer / fx / api / server
│  └─ tests/           #   unittest，真跑 HTTP 往返（数据来自 seed_demo）
├─ frontend/           # Vue 3 + Vite + TS + Pinia + Element Plus + ECharts
│  └─ src/mock/        #   dev-only mock（演示数据驱动，生产构建被摇掉）
├─ mcp/                # 纯标准库 MCP 桥接（只转发到后端 HTTP API）
├─ tools/seed_demo.py  # 生成演示数据 + 创建演示账号（唯一的"数据来源"）
├─ scripts/xlsx_lite.py# 标准库 xlsx 读取（供自行编写 Excel 导入脚本复用）
├─ docs/m1-spec.md     # 接口契约与实施约定
├─ .githooks/pre-commit# 提交前门禁：真实数据永不入库
└─ deploy/             # systemd unit、nginx vhost 模板、渲染脚本、备份与发布脚本
```

## 功能一览

> 以下截图全部来自本仓库自带的演示数据（`tools/seed_demo.py` 生成），**不含任何真实财务信息**。

**净值看板** —— 当前净资产、环比 / 近 12 月涨幅 / 美元占比 / 负债率四张 KPI，
净值曲线（净资产 / 总资产 / 总负债）、资产负债结构环形图与前 10 大持仓。

![净值看板](docs/screenshots/01-dashboard.webp)

**快照录入** —— 从上一期一键结转，只改需要改的行；小计与净资产由服务端重算，
偏离过大或符号异常的行只打标记待复核，不阻塞提交。

![快照录入](docs/screenshots/02-snapshot.webp)

**账户台账** —— 账户与别名归一（同一账户的历史写法合并），分类 / 子类、
最近金额、出现期数、是否计入统计，支持新建 / 改名 / 加别名。

![账户台账](docs/screenshots/03-accounts.webp)

**账户详情** —— 单账户历史序列与逐期环比，快速看出某笔钱的变化轨迹。

![账户详情](docs/screenshots/04-account-detail.webp)

**投资日志** —— 把明细行备注按「期 + 文本」聚合成**期级投资日志**（同期多账户引用的同一段文字去重）
与**账户备注**，支持按作用域 / 账户 / 关键词过滤。

![投资日志](docs/screenshots/05-journal.webp)

**数据导入 / 导出** —— 导入自己的历史数据，或导出 JSON 备份（导出可原样导入还原）。

![数据导入](docs/screenshots/06-import.webp)

**设置** —— 汇率抓取、账户与分类维护、口令修改等。

![设置](docs/screenshots/07-settings.webp)

**登录** —— 单用户登录；首个账号注册后自动关闭注册（需要时用 `ALLOW_REGISTRATION=1` 打开）。

![登录](docs/screenshots/08-login.webp)

**地点与定位** —— 快照带一等 `location` 字段（城市级文本 + 可选坐标，`source ∈ manual/carried/device/parsed_note`）。
录入页可一键取当前定位；结转时自动带入最近一期有地点的值，**永不覆盖你手改过的值**；
没记录的期是「键缺失 + 原因码」，不是空字符串或 0。逆地理由服务端代理（默认关闭）、结果缓存、
失败返回明确的 4xx/5xx，绝不编造地点。契约见 `docs/m1-spec.md` §8。

**MCP 桥接** —— `mcp/` 是纯标准库实现的 MCP 服务，把工具调用转发给后端 HTTP API
（后端始终是唯一写者，MCP 进程不碰 SQLite），默认只读、只监听回环。

## 计算口径

**净资产 = Σ 该期所有明细行的有符号 `amount_cny`**（负债块里的正号行照原样相加）。

- 原有的 `小计 / 资产总计 / 净资产` 汇总行一律**不信任**，服务端每次重算；
- 符号由账户的 `kind`（asset / liability）决定，不信任来源数据的正负号；
- 单行金额相对上期偏离过大或符号异常时，只打标记待复核，不阻塞提交。

## 快速开始

```bash
# 1) 生成演示数据 + 创建演示账号（口令会打印在终端）
cd backend
python3 ../tools/seed_demo.py --db data/networth.db
#    可选：--periods 24 --invest-accounts 12 --seed 20260101 --user demo --password 自定义

# 2) 起服务（默认 127.0.0.1:8972）
./run.sh                        # 或 NETWORTH_PORT=8972 python3 -m app.server

# 3) 前端
cd ../frontend
npm install                     # 或 NODE_MODULES_DIR=/path/to/node_modules bash scripts/build.sh
bash scripts/build.sh           # vue-tsc 类型检查 + vite build → dist/
```

打开页面用第 1 步打印的账号登录。首个账号创建后注册自动关闭
（需要再开注册时设置 `ALLOW_REGISTRATION=1`）。

## 导入你自己的数据

服务端只认一组 M0 风格的 JSON（`accounts.json` / `snapshots.json` / `reconcile.json`
/ `adjustments.json`，字段含义见 `backend/app/importer.py` 顶部注释与 `docs/m1-spec.md`）：

```bash
cd backend
python3 -m app.importer --source /path/to/data-out --db data/networth.db   # 幂等
python3 -m app.importer --source /path/to/data-out --db data/networth.db --reset  # 改名/拆分账户后重建
```

`scripts/xlsx_lite.py` 提供纯标准库的 xlsx 读取，可用来写自己的 Excel → JSON 转换脚本
（本仓库不附带任何针对特定表格的解析脚本，那部分与本项目使用者的原始表格强相关）。

## 测试

```bash
cd backend && /usr/bin/python3 -m unittest discover -s tests -v
```

测试在运行时用 `tools/seed_demo.py` 生成一份确定性合成数据集（含重复行、正号负债、
附加表、总览缺记录、用户确认修正等边界），所有断言都相对这份数据集做结构校验，
不依赖任何固定金额或真实数据。**这是硬要求**：`tests/` 下任何写死金额/日期/账户名、
或直接读 `data/out/` 的测试都算门禁不通过。

## 提交纪律（真实数据永不入历史）

本仓库的设计前提是「**代码与数据完全分离**」：真实数据只存在于使用者本机
（`data/source/`、`data/out/`、`backend/data/`、`deploy/local.env`、`deploy/rendered/`），
这些路径全部在 `.gitignore` 里，仓库里没有 `.json` 数据文件、没有 `.xlsx`、没有 `.db`。

一次习惯性的 `git add -A` 就足以把真实金额、真实账户名与真实地点写进历史，事后只能重写历史补救。
所以本仓库自带一道本地闸：

```bash
git config core.hooksPath .githooks   # 每个 clone 各执行一次
```

`.githooks/pre-commit` 会拒绝：`data/`、`backend/data/`、`deploy/local.env`、`deploy/rendered/`、
`docs/private/` 等真实数据路径，`*.db`/`*.xlsx`，以及非演示数据来源的
`frontend/src/mock/fixtures.json`（payload 里的 `"source"` 必须是 `"demo"`）。
如果你还想拦自己本地的真实金额/账户名/地点关键词，把它们写成一行正则放进
`.githooks/private-patterns`（该文件已被 gitignore，只在你本机生效），钩子会一并扫描**新增行**。

## 部署

```bash
# 0) 机器相关值只写在本机（不入库），然后用它渲染模板
cp deploy/local.env.example deploy/local.env && "${EDITOR:-vi}" deploy/local.env
bash deploy/render.sh            # → deploy/rendered/{nginx.conf,*service}（不入库）
```

```bash
# 前端产物 → nginx 静态目录
WEB_ROOT=/var/www/networth bash deploy/deploy-web.sh

# API → systemd（用渲染后的 unit：<PROJECT_ROOT>/<BACKUP_DIR> 已替换）
sudo cp deploy/rendered/networth-api.service /etc/systemd/system/
sudo systemctl enable --now networth-api

# vhost（渲染后的文件已含 /api 反代与 SPA 回退）
sudo cp deploy/rendered/nginx.conf /etc/nginx/conf.d/networth.conf
sudo nginx -t && sudo nginx -s reload

# 每日备份（走 sqlite3 backup API，WAL 下安全）
sudo cp deploy/rendered/networth-backup.service deploy/networth-backup.timer /etc/systemd/system/
sudo systemctl enable --now networth-backup.timer
```

配置全部走环境变量（见 `backend/app/config.py`）：`NETWORTH_DB`、`NETWORTH_SECRET`、
`NETWORTH_PORT`、`NETWORTH_BIND`、`ALLOW_REGISTRATION`、`NETWORTH_WEB_DIST`。

## 运维注意

- **SQLite 是 WAL 模式**：运行中已提交的数据可能还在 `networth.db-wal` 里，直接
  `cp networth.db backup.db` 会复制到旧状态。请用 `python3 deploy/backup.py`（sqlite3
  backup API）；服务优雅退出时会执行 `PRAGMA wal_checkpoint(TRUNCATE)`，停服后的 `.db`
  是完整单文件。
- **`NETWORTH_SECRET` 不要更换**：口令哈希把该密钥当 pepper，密钥一变已有账号的密码
  就永久无法校验（没有找回入口）。库里 `meta.secret_fingerprint` 记录首次注册时的指纹，
  不一致时 `/api/healthz` 会返回 `"secret_mismatch": true` 并在日志里告警。
- **账户改名/拆分后必须 `--reset` 重新导入**：否则旧账户行会残留，账户数虚增。
- **投资日志来自明细备注**：同一期的同一段文字出现在多个账户行时，服务端按「期 + 文本」
  聚合去重，出现 ≥2 次判为**期级投资日志**，只出现 1 次判为**账户备注**（`/api/journal`）。
- **汇率**：`GET /api/fx?date=YYYY-MM-DD` 取该日 USD→CNY（主源 ECB 中间价，兜底 open.er-api），
  结果缓存到 `fx_rate` 表，`refresh=1` 强制重抓。**抓取失败返回 502，绝不返回编造值**；
  超出合理区间的返回值一律拒绝。周末/节假日取最近前一交易日并如实返回 `source_date` / `stale_days`。
- **历史汇率回填**：`cd backend && python3 -m app.fx_backfill`（默认只报告），`--apply`
  只补空缺期，已填写的值不动。
- **地点与逆地理**：`NETWORTH_GEOCODE_PROVIDER=off`（默认）|`nominatim`|`amap`；关闭时
  `/api/location/reverse` 返回 409 而不是假装成功。坐标出网前按约 1km 网格粗化后查缓存，
  失败返回 502 且**不写任何值**。地点写库有专用窄接口
  （`PUT/POST /api/snapshots/{date}/location`）：**整期写入漏 `items` 会被 400 拒绝**，
  因为整期 upsert 是「删明细 + 重插」，漏 `items` 会静默清空那一期的明细。
- **MCP**：`mcp/` 只经 HTTP 调后端，密钥（`NETWORTH_MCP_TOKEN`）只放在
  `backend/.env`。systemd unit 里**不要**再写一行空的 `Environment=NETWORTH_MCP_TOKEN=`——
  systemd 后出现的 `Environment=` 会盖掉 `EnvironmentFile`，token 被清空后每次调用都是 401。

## License

MIT
