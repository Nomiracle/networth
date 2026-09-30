"""networth 后端验收测试（unittest，全部真跑：真实 SQLite + 真实 HTTP 往返）。

数据来源：`tools/seed_demo.py` 在测试运行时生成的合成数据集（固定随机种子），
**仓库内不含任何数据文件**。所有断言都相对生成的数据集做结构校验，
不绑定任何固定金额或账户名。

运行：
    cd backend && /usr/bin/python3 -m unittest discover -s tests -v

覆盖 docs/m1-spec.md 第 5 节后端全部验收点：
  * /api/healthz / 鉴权 / 注册（首账号后自动关闭）
  * /api/snapshots/{date} 净资产 = 资产 + 负债，且等于数据集 computed
  * /api/metrics/networth 逐期等于数据集 computed
  * 对账端点：总览无记录的期 imported/diff 为 null；用户确认修正的期计入 adjusted
  * 导入质量标记：同额重复行已合并 / 正号行 sign_anomaly / extra_table
  * carry-forward -> 保存新快照 -> 净值点增加 -> 删除
  * /api/export -> 导入全新 DB -> 各表计数一致
  * 投资日志聚合、密钥指纹守卫、美元录入折算、汇率缓存与失败语义
测试用的临时库与临时端口均在 tearDownClass 关闭，不留常驻服务。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
for extra in (BACKEND, ROOT / "tools"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import seed_demo  # noqa: E402  （演示数据生成器，位于 tools/）
from app import api, config, domain, importer  # noqa: E402
from app.store import Store  # noqa: E402

# 数据集在 setUpClass 里生成；SOURCE 指向生成的 JSON 目录
SOURCE: Path = Path()
META: dict = {}
PERIODS = 24
SEED = 20260101
ADMIN = ("验收管理员", "networth-test-pass")
TTL_TOKEN = None  # 由 setUpClass 填充


def reconcile_rows() -> dict[str, dict]:
    """读取本次生成的 M0 对账结果，用于逐期比对。"""
    raw = json.loads((SOURCE / "reconcile.json").read_text(encoding="utf-8"))
    return {str(r["date"]): r for r in raw if r.get("date")}


def anchors() -> dict[str, float]:
    """锚点 = 数据集前/中/末期由生成器给出的 computed（不写死金额）。"""
    dates = META["dates"]
    picks = (dates[0], dates[len(dates) // 2], dates[-1])
    return {d: META["computed"][d] for d in picks}


class ServerHandle:
    """临时 HTTP 服务（随机端口，用后即关）。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.store = Store(db_path)
        self.settings = config.load()
        self.server = api.build_server(self.settings, self.store, host="127.0.0.1", port=0)
        self.port = int(self.server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()

    # -- HTTP 工具
    def request(self, method: str, path: str, body=None, token: str | None = None):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return status, payload

    def get(self, path, token=None):
        return self.request("GET", path, None, token)


class NetworthApiTests(unittest.TestCase):
    """全部测试共用一个已导入 M0 数据的临时库 + 一个临时 HTTP 服务。"""

    @classmethod
    def setUpClass(cls) -> None:
        global SOURCE, META
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="networth-test-"))
        cls.db = cls.tmpdir / "networth.db"
        # 生成合成数据集（仓库不含数据文件；同一 seed → 同一份数据）
        SOURCE = cls.tmpdir / "dataset"
        META = seed_demo.generate_dataset(SOURCE, periods=PERIODS, seed=SEED)
        os.environ["NETWORTH_DB"] = str(cls.db)
        os.environ["NETWORTH_SECRET"] = "unit-test-secret"
        os.environ["NETWORTH_M0_OUT"] = str(SOURCE)
        os.environ.pop("ALLOW_REGISTRATION", None)

        # 1) 先跑一次真实的导入 CLI（捕获输出用于校验摘要行）
        cls.import_stdout = io.StringIO()
        with contextlib.redirect_stdout(cls.import_stdout):
            cls.import_rc = importer.main(["--source", str(SOURCE), "--db", str(cls.db)])
        assert cls.import_rc == 0, "导入应返回 0"

        # 2) 起临时服务
        cls.srv = ServerHandle(cls.db)
        # 3) 首个账号注册 + 登录（注册后应自动关闭注册）
        status, _ = cls.srv.request("POST", "/api/auth/register",
                                    {"username": ADMIN[0], "password": ADMIN[1]})
        assert status == 201, f"首个账号注册应成功，实际 {status}"
        status, payload = cls.srv.request("POST", "/api/auth/login",
                                          {"username": ADMIN[0], "password": ADMIN[1]})
        assert status == 200, f"登录应成功，实际 {status}"
        cls.token = payload["token"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)
        for key in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
            os.environ.pop(key, None)

    # ------------------------------------------------------------ 导入与健康检查

    def test_01_importer_summary_line(self) -> None:
        """导入 CLI 摘要行与数据集规模一致，且对账全部通过。"""
        out = self.import_stdout.getvalue()
        last = [line for line in out.strip().splitlines() if line.startswith("accounts=")][-1]
        expected = (f"accounts={META['account_count']} snapshots={META['period_count']} "
                    f"items={META['db_item_count']} adjusted=1 mismatched=0")
        self.assertEqual(last, expected)
        self.assertIn("用户确认修正 1 期", out)

    def test_02_healthz(self) -> None:
        """/api/healthz 无需鉴权，返回期数与账户数。"""
        status, payload = self.srv.get("/api/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["periods"], META["period_count"])
        self.assertEqual(payload["accounts"], META["account_count"])
        self.assertEqual(payload["items"], META["db_item_count"])

    # ------------------------------------------------------------ 鉴权

    def test_03_auth_required(self) -> None:
        """除 healthz/auth 外都要 Bearer 令牌。"""
        for path in ("/api/accounts", "/api/snapshots", "/api/metrics/networth", "/api/export"):
            status, payload = self.srv.get(path)
            self.assertEqual(status, 401, path)
            self.assertEqual(payload["error"]["code"], "unauthorized")
        status, payload = self.srv.get("/api/accounts", token="not-a-real-token")
        self.assertEqual(status, 401)

    def test_04_registration_closed_after_first_user(self) -> None:
        """首账号注册成功后，未设置 ALLOW_REGISTRATION 时注册自动关闭。"""
        status, payload = self.srv.request("POST", "/api/auth/register",
                                           {"username": "第二个人", "password": "whatever123"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "registration_closed")

    def test_05_login_me_logout(self) -> None:
        """登录 -> /api/me -> 注销后令牌立即失效。"""
        status, payload = self.srv.request("POST", "/api/auth/login",
                                           {"username": ADMIN[0], "password": "错误密码"})
        self.assertEqual(status, 401)

        status, session = self.srv.request("POST", "/api/auth/login",
                                           {"username": ADMIN[0], "password": ADMIN[1]})
        self.assertEqual(status, 200)
        temp_token = session["token"]
        self.assertTrue(session["expires_at"])

        status, me = self.srv.get("/api/me", token=temp_token)
        self.assertEqual(status, 200)
        self.assertEqual(me["username"], ADMIN[0])

        # 令牌以 sha256 摘要入库：库中不得出现明文
        digest = hashlib.sha256(temp_token.encode("utf-8")).hexdigest()
        row = self.srv.store.find_token(digest)
        self.assertIsNotNone(row, "库中应存在 token 的 sha256 摘要")
        self.assertIsNone(self.srv.store.find_token(temp_token), "明文令牌不得入库")
        self.assertNotIn(temp_token, str(dict(row or {})))

        status, out = self.srv.request("POST", "/api/auth/logout", {}, token=temp_token)
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        status, _ = self.srv.get("/api/me", token=temp_token)
        self.assertEqual(status, 401, "注销后的令牌必须失效")

    # ------------------------------------------------------------ 账户

    def test_06_accounts_list(self) -> None:
        """账户台账：数量、资产/负债拆分、模板行不计入统计、别名齐备。"""
        status, payload = self.srv.get("/api/accounts", token=self.token)
        self.assertEqual(status, 200)
        accounts = payload["accounts"]
        self.assertEqual(len(accounts), META["account_count"])
        want_asset = sum(1 for a in META["accounts"] if a["kind"] == "asset")
        want_liab = sum(1 for a in META["accounts"] if a["kind"] == "liability")
        self.assertEqual(sum(1 for a in accounts if a["kind"] == "asset"), want_asset)
        self.assertEqual(sum(1 for a in accounts if a["kind"] == "liability"), want_liab)

        template = [a for a in accounts if a["name"] == META["template_account"]]
        self.assertEqual(len(template), 1)
        self.assertEqual(template[0]["is_counted"], 0, "模板行不计入统计")
        self.assertEqual(template[0]["periods"], 0)

        dot = [a for a in accounts if a["name"] == "投资账户01"][0]
        self.assertGreaterEqual(len(dot["aliases"]), 2)
        self.assertEqual(dot["last_date"], META["dates"][-1])
        self.assertIsNotNone(dot["last_amount"])
        self.assertEqual(dot["periods"], META["period_count"], "首个账户每期都有一行")

        # 生成器里前两个账户各带 2 个别名，其余各 1 个
        by_name = {a["name"]: a for a in accounts}
        self.assertEqual(len(by_name["投资账户01"]["aliases"]), 2)
        self.assertGreaterEqual(by_name["银行账户01"]["unchanged_tail"], 0)
        # 激活较晚的账户期数应少于首个账户
        self.assertLess(by_name["私人借款01"]["periods"], by_name["投资账户01"]["periods"])

    def test_17_account_crud_and_series(self) -> None:
        """新建账户 / PATCH / 加别名 / 冲突 409 / 序列。"""
        status, created = self.srv.request("POST", "/api/accounts",
                                           {"name": "验收临时账户", "kind": "asset",
                                            "category": "金融资产"}, token=self.token)
        self.assertEqual(status, 201, created)
        account_id = created["id"]

        status, dup = self.srv.request("POST", "/api/accounts",
                                       {"name": "验收临时账户", "kind": "asset"}, token=self.token)
        self.assertEqual(status, 409)
        self.assertEqual(dup["error"]["code"], "conflict")

        status, patched = self.srv.request("PATCH", f"/api/accounts/{account_id}",
                                           {"subclass": "验收", "is_counted": 1}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(patched["subclass"], "验收")

        status, alias = self.srv.request("POST", f"/api/accounts/{account_id}/aliases",
                                         {"alias": "验收别名"}, token=self.token)
        self.assertEqual(status, 201)
        status, _ = self.srv.request("POST", f"/api/accounts/{account_id}/aliases",
                                     {"alias": "验收别名"}, token=self.token)
        self.assertEqual(status, 409, "别名重复应 409")

        # 序列：拿一个历史账户
        accounts = self.srv.get("/api/accounts", token=self.token)[1]["accounts"]
        target = [a for a in accounts if a["name"] == "银行账户01"][0]
        status, series = self.srv.get(f"/api/accounts/{target['id']}/series", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(series["points"]), target["periods"])
        self.assertIsNone(series["points"][0]["delta"])
        self.assertAlmostEqual(series["points"][-1]["amount"], target["last_amount"], places=2)
        # 详情页用到的账户字段必须与列表一致（曾缺失 → 页面显示「期数 0、计入统计 否」）
        self.assertEqual(series["account"]["periods"], target["periods"])
        self.assertEqual(series["account"]["unchanged_tail"], target["unchanged_tail"])
        self.assertEqual(series["account"]["last_date"], target["last_date"])
        self.assertEqual(int(series["account"]["is_counted"]), int(target["is_counted"]))
        self.assertEqual(series["account"]["aliases"], target["aliases"])

        status, _ = self.srv.get("/api/accounts/999999/series", token=self.token)
        self.assertEqual(status, 404)

    # ------------------------------------------------------------ 锚点与口径

    def test_08_snapshot_detail_math(self) -> None:
        """锚点（数据集前/中/末期）精确命中，且 total_assets + total_liabilities == networth。"""
        for date_text, expected in anchors().items():
            status, payload = self.srv.get(f"/api/snapshots/{date_text}", token=self.token)
            self.assertEqual(status, 200, date_text)
            snap = payload["snapshot"]
            self.assertAlmostEqual(snap["networth"], expected, places=2, msg=date_text)
            self.assertEqual(round(snap["networth"], 2), expected)
            self.assertAlmostEqual(snap["total_assets"] + snap["total_liabilities"],
                                   snap["networth"], places=6)
            self.assertEqual(snap["item_count"], len(payload["items"]))

    def test_09_networth_metrics_matches_reconcile(self) -> None:
        """逐期净值等于生成数据集的 computed。"""
        expected = reconcile_rows()
        status, payload = self.srv.get("/api/metrics/networth", token=self.token)
        self.assertEqual(status, 200)
        points = payload["points"]
        self.assertEqual(len(points), META["period_count"])

        mismatched = []
        for point in points:
            want = expected[point["date"]]["computed"]
            if round(point["networth"], 2) != round(want, 2):
                mismatched.append((point["date"], point["networth"], want))
        self.assertEqual(mismatched, [], "逐期净值必须与数据集 computed 完全一致")

        self.assertEqual(payload["kpi"]["periods"], META["period_count"])
        self.assertEqual(payload["kpi"]["current"], points[-1]["networth"])
        self.assertIsNotNone(payload["kpi"]["debt_ratio"])
        self.assertIsNotNone(payload["kpi"]["usd_share"])

    def test_10_reconcile_endpoint(self) -> None:
        """对账端点：总览无记录的期 imported/diff 为 null；修正期差值为预期值。"""
        status, payload = self.srv.get("/api/metrics/reconcile", token=self.token)
        self.assertEqual(status, 200)
        periods = {p["date"]: p for p in payload["periods"]}
        self.assertEqual(len(periods), META["period_count"])

        row = periods[META["no_overview_date"]]
        self.assertIsNone(row["imported"])
        self.assertIsNone(row["diff"])
        self.assertEqual(row["computed"], META["computed"][META["no_overview_date"]])

        adj = periods[META["adjusted_date"]]
        self.assertAlmostEqual(adj["imported"], adj["computed"] + 500.0, places=2)

        self.assertEqual(sum(1 for p in periods.values() if p["imported"] is not None),
                         META["period_count"] - 1)

    def test_11_import_flags_preserved(self) -> None:
        """data_quality_flags 落库：同额重复行合并 / extra_table / 正号行。"""
        payload = self.srv.get(f"/api/snapshots/{META['dup_date']}", token=self.token)[1]
        flags = payload["snapshot"]["data_quality_flags"]
        self.assertTrue(any("同额重复行已去重" in f or "多行已合并" in f for f in flags), flags)
        self.assertTrue(any("同额多行" in f for f in flags), flags)
        # 合并后同一账户只保留一行，且该期净值仍等于数据集 computed
        grouped: dict[str, int] = {}
        for item in payload["items"]:
            grouped[item["account"]] = grouped.get(item["account"], 0) + 1
        self.assertTrue(all(n == 1 for n in grouped.values()), "同账户只应保留合并后的一行")
        self.assertAlmostEqual(payload["snapshot"]["networth"],
                               META["computed"][META["dup_date"]], places=2)

        flags_extra = self.srv.get(f"/api/snapshots/{META['extra_date']}",
                                   token=self.token)[1]["snapshot"]["data_quality_flags"]
        self.assertIn("extra_table", flags_extra)

        # 负债块正号行不能取绝对值/归零
        items = self.srv.get(f"/api/snapshots/{META['sign_date']}", token=self.token)[1]["items"]
        positives = [i for i in items if i["kind"] == "liability" and i["amount_cny"] > 0]
        self.assertTrue(positives, "负债正号行必须保留原符号")
        self.assertTrue(any("sign_anomaly" in (i["flags"] or []) for i in positives))

    def test_12_structure_and_diff(self) -> None:
        """结构与环比对比端点。"""
        last, prev = META["dates"][-1], META["dates"][-2]
        status, payload = self.srv.get("/api/metrics/structure", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["date"], last)
        self.assertTrue(payload["groups"])
        self.assertTrue(all(g["share"] is not None for g in payload["groups"]))
        self.assertGreater(len(payload["liabilities"]), 0)

        status, diff = self.srv.get(f"/api/snapshots/{last}/diff?against={prev}",
                                    token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(diff["against"], prev)
        after, before = META["computed"][last], META["computed"][prev]
        self.assertAlmostEqual(diff["totals"]["after"], after, places=2)
        self.assertAlmostEqual(diff["totals"]["before"], before, places=2)
        self.assertAlmostEqual(diff["totals"]["delta"], round(after - before, 2), places=2)

    # ------------------------------------------------------------ 写入流程（carry-forward）

    def test_13_carry_forward_save_and_delete(self) -> None:
        """carry-forward -> 保存新快照 -> 净值点增加 -> 删除后恢复。"""
        last = META["dates"][-1]
        before = self.srv.get("/api/metrics/networth", token=self.token)[1]["points"]
        self.assertEqual(len(before), META["period_count"])

        status, cf = self.srv.request("POST", "/api/snapshots/carry-forward",
                                      {"from": last}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(cf["source_date"], last)
        self.assertTrue(cf["date_default"])
        self.assertGreater(len(cf["items"]), 0)
        self.assertTrue(all(i["auto_filled"] == 1 for i in cf["items"]))

        new_date = "2026-06-15"
        items = [{"account_id": i["account_id"],
                  "amount_cny": i["amount_cny"] + (1000 if i["kind"] == "asset" else 0),
                  "fx_rate": i["fx_rate"], "auto_filled": 1}
                 for i in cf["items"]]
        expected_nw = round(sum(i["amount_cny"] for i in items), 2)
        status, saved = self.srv.request("PUT", f"/api/snapshots/{new_date}",
                                         {"date": new_date, "default_fx_rate": cf["fx_rate_default"],
                                          "market_note": "验收用快照", "items": items},
                                         token=self.token)
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["created"])
        self.assertEqual(saved["snapshot"]["item_count"], len(items))
        self.assertAlmostEqual(saved["snapshot"]["networth"], expected_nw, places=2)

        after = self.srv.get("/api/metrics/networth", token=self.token)[1]["points"]
        self.assertEqual(len(after), META["period_count"] + 1, "净值点必须增加 1")
        self.assertEqual(after[-1]["date"], new_date)
        self.assertEqual(round(after[-1]["networth"], 2), expected_nw)
        self.assertGreater(after[-1]["delta"], 0)

        # 同一天用 POST 覆盖（upsert 语义）+ 再次 PUT 幂等
        status, again = self.srv.request("POST", "/api/snapshots",
                                         {"date": new_date, "items": items}, token=self.token)
        self.assertEqual(status, 200)
        self.assertFalse(again["created"], "同日再次提交应为 upsert 而非新建")
        self.assertEqual(
            len(self.srv.get("/api/metrics/networth", token=self.token)[1]["points"]),
            META["period_count"] + 1)

        status, deleted = self.srv.request("DELETE", f"/api/snapshots/{new_date}", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(deleted["ok"])
        restored = self.srv.get("/api/metrics/networth", token=self.token)[1]["points"]
        self.assertEqual(len(restored), META["period_count"])
        status, _ = self.srv.request("DELETE", f"/api/snapshots/{new_date}", token=self.token)
        self.assertEqual(status, 404)

    # ------------------------------------------------------------ 导出 / 导入

    def test_14_export_import_roundtrip(self) -> None:
        """GET /api/export -> POST /api/import 到全新 DB -> 各表计数一致。"""
        live_accounts = self.srv.get("/api/accounts", token=self.token)[1]["accounts"]
        live_snapshots = self.srv.get("/api/snapshots", token=self.token)[1]["snapshots"]
        status, exported = self.srv.get("/api/export", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(exported["version"], 1)
        self.assertEqual(len(exported["accounts"]), len(live_accounts))
        self.assertEqual(len(exported["snapshots"]), len(live_snapshots))
        self.assertIn("exported_at", exported)

        other_db = self.tmpdir / "imported.db"
        other = ServerHandle(other_db)
        try:
            status, _ = other.request("POST", "/api/auth/register",
                                      {"username": "导入验证", "password": "import-pass-1"})
            self.assertEqual(status, 201)
            _, session = other.request("POST", "/api/auth/login",
                                       {"username": "导入验证", "password": "import-pass-1"})
            status, result = other.request("POST", "/api/import", exported, token=session["token"])
            self.assertEqual(status, 200, result)
            self.assertEqual(result["counts"]["account"], len(live_accounts))
            self.assertEqual(result["counts"]["snapshot"], len(live_snapshots))
            self.assertEqual(result["counts"]["snapshot_item"], META["db_item_count"])
            self.assertGreater(result["counts"]["account_alias"], 0)

            _, health = other.get("/api/healthz")
            self.assertEqual(health["periods"], len(live_snapshots))
            self.assertEqual(health["accounts"], len(live_accounts))

            _, re_exported = other.get("/api/export", token=session["token"])
            self.assertEqual(len(re_exported["accounts"]), len(exported["accounts"]))
            self.assertEqual(len(re_exported["snapshots"]), len(exported["snapshots"]))

            # 幂等：再导入一次计数不变
            status, again = other.request("POST", "/api/import", exported, token=session["token"])
            self.assertEqual(status, 200)
            self.assertEqual(again["counts"]["snapshot_item"], META["db_item_count"])

            # 锚点在全新库里同样命中
            last = META["dates"][-1]
            _, anchor = other.get(f"/api/snapshots/{last}", token=session["token"])
            self.assertEqual(round(anchor["snapshot"]["networth"], 2), META["computed"][last])

            # 新库没有 reconcile_period 行，端点应回退读生成的 reconcile-imported.json
            status, recon = other.get("/api/metrics/reconcile", token=session["token"])
            self.assertEqual(status, 200)
            self.assertEqual(len(recon["periods"]), META["period_count"])
            self.assertIsNone({p["date"]: p for p in recon["periods"]}[META["no_overview_date"]]["imported"])
        finally:
            other.close()

    def test_15_importer_idempotent(self) -> None:
        """再跑一次导入 CLI，计数不因重复导入而变化（幂等）。"""
        counts_before = self.srv.store.counts()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            rc = importer.main(["--source", str(SOURCE), "--db", str(self.db)])
        self.assertEqual(rc, 0)
        summary = [line for line in buffer.getvalue().strip().splitlines()
                   if line.startswith("accounts=")][-1]
        counts = self.srv.store.counts()
        self.assertEqual(summary,
                         f"accounts={counts['account']} snapshots={counts['snapshot']} "
                         f"items={counts['snapshot_item']} adjusted=1 mismatched=0")
        for key in ("account", "snapshot", "snapshot_item"):
            self.assertEqual(counts[key], counts_before[key], key)

    # ------------------------------------------------------------ 错误处理

    def test_16_error_contract(self) -> None:
        """错误统一 {error:{code,message}}，状态码 400/404/405/409。"""
        status, payload = self.srv.get("/api/snapshots/2001-01-01", token=self.token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

        status, payload = self.srv.get("/api/snapshots/2025-13-45", token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "bad_date")

        status, payload = self.srv.request("POST", "/api/snapshots",
                                          {"date": "2025-09-02", "items": [
                                              {"account_id": 999999, "amount_cny": 1}]},
                                          token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("account_id", payload["error"]["message"])

        accounts = self.srv.get("/api/accounts", token=self.token)[1]["accounts"]
        aid = accounts[0]["id"]
        status, payload = self.srv.request("POST", "/api/snapshots",
                                          {"date": "2025-09-02", "items": [
                                              {"account_id": aid, "amount_cny": 1},
                                              {"account_id": aid, "amount_cny": 2}]},
                                          token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("重复", payload["error"]["message"])

        status, payload = self.srv.get("/api/unknown-endpoint", token=self.token)
        self.assertEqual(status, 404)

        status, payload = self.srv.request("DELETE", "/api/accounts", None, token=self.token)
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    # ------------------------------------------------------------ 投资日志

    def test_18_journal_aggregation(self) -> None:
        """投资日志：同期同文本备注聚合去重，并区分「期级日志」与「账户备注」。"""
        status, data = self.srv.get("/api/journal", token=self.token)
        self.assertEqual(status, 200, data)
        entries = data["entries"]

        # 数据集里有一期把同一段日志写在多个账户行上 → 聚合成 1 条，且判为期级日志
        big = [e for e in entries if e["date"] == META["journal_date"] and e["scope"] == "journal"]
        self.assertEqual(len(big), 1, "同期同文本备注必须聚合成一条")
        self.assertEqual(big[0]["account_count"], len(META["journal_accounts"]))
        self.assertIn("示例投资日志", big[0]["text"])
        self.assertFalse(big[0]["text"].startswith("备注"), big[0]["text"][:20])

        # 单账户备注不该被判成投资日志
        owed = [e for e in entries if "示例账户备注" in e["text"]]
        self.assertTrue(owed)
        self.assertTrue(all(e["scope"] == "account" for e in owed))

        # scope 过滤
        status, only_acc = self.srv.get("/api/journal?scope=account", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(all(e["scope"] == "account" for e in only_acc["entries"]))
        self.assertEqual(only_acc["journal_count"], 0)

        # 账户过滤
        target_id = owed[0]["accounts"][0]["id"]
        status, by_acc = self.srv.get(f"/api/journal?account_id={target_id}", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(by_acc["entries"])
        self.assertTrue(all(any(x["id"] == target_id for x in e["accounts"]) for e in by_acc["entries"]))

        # 关键词过滤（中文需 URL 编码）
        status, kw = self.srv.get(f"/api/journal?q={urllib.parse.quote('示例账户备注')}", token=self.token)
        self.assertTrue(kw["entries"])
        self.assertTrue(all("示例账户备注" in e["text"] for e in kw["entries"]))

        # 参数校验与鉴权
        status, payload = self.srv.get("/api/journal?scope=oops", token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "bad_request")
        status, payload = self.srv.get("/api/journal?account_id=abc", token=self.token)
        self.assertEqual(status, 400)
        status, _ = self.srv.get("/api/journal")
        self.assertEqual(status, 401)

    # ------------------------------------------------------------ 密钥指纹

    def test_19_secret_fingerprint_guard(self) -> None:
        """密钥指纹：一致 → False；换掉 NETWORTH_SECRET → True（会导致已有账号无法登录）。"""
        status, h = self.srv.get("/api/healthz")
        self.assertEqual(status, 200)
        self.assertIs(h.get("secret_mismatch"), False)

        with tempfile.TemporaryDirectory(prefix="networth-secret-") as tmp:
            st = Store(Path(tmp) / "s.db")
            try:
                base = config.load()
                st.set_meta(domain.SECRET_META_KEY, domain.secret_fingerprint(base))
                self.assertFalse(domain.secret_mismatch(st, base))
                other = replace(base, secret=b"another-secret-entirely")
                self.assertTrue(domain.secret_mismatch(st, other), "换密钥必须被发现")
                self.assertIsNone(st.get_meta("不存在的键"))
            finally:
                st.close()


    # ------------------------------------------------------------ 美元录入

    def test_20_usd_entry_derives_cny(self) -> None:
        """只给 amount_usd（+汇率）时：服务端折算 amount_cny，负债取负，美元字段保留。"""
        accounts = self.srv.get("/api/accounts", token=self.token)[1]["accounts"]
        asset = [a for a in accounts if a["kind"] == "asset"][0]
        liab = [a for a in accounts if a["kind"] == "liability"][0]
        date = "2026-01-15"
        status, payload = self.srv.request("POST", "/api/snapshots", {
            "date": date,
            "default_fx_rate": 7.2,
            "items": [
                {"account_id": asset["id"], "amount_usd": 1000, "fx_rate": 7.2, "note": "美元行"},
                {"account_id": liab["id"], "amount_usd": 100},   # 不给行级汇率 → 用期默认汇率
            ],
        }, token=self.token)
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["created"], "新期次应标记 created")
        items = {i["account_id"]: i for i in payload["items"]}
        self.assertAlmostEqual(items[asset["id"]]["amount_cny"], 7200.0, places=2)
        self.assertAlmostEqual(items[asset["id"]]["amount_usd"], 1000.0, places=2)
        # 负债：人民币为负、美元为正（与原表 16 行借币数据的符号约定一致）
        self.assertAlmostEqual(items[liab["id"]]["amount_cny"], -720.0, places=2)
        self.assertAlmostEqual(items[liab["id"]]["amount_usd"], 100.0, places=2)

        # 该行的美元标记要能驱动「美元资产占比」KPI（7200/7200 = 100%）
        status, metrics = self.srv.get("/api/metrics/networth", token=self.token)
        self.assertAlmostEqual(metrics["kpi"]["current"], 6480.0, places=2)
        self.assertAlmostEqual(metrics["kpi"]["usd_share"], 100.0, places=2)

        # 两个金额都不给 → 400
        status, err = self.srv.request("POST", "/api/snapshots", {
            "date": "2026-01-16", "items": [{"account_id": asset["id"]}],
        }, token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("amount_cny", err["error"]["message"])

        # 清理：不让这期影响其它断言
        status, _ = self.srv.request("DELETE", f"/api/snapshots/{date}", None, token=self.token)
        self.assertEqual(status, 200)


    # ------------------------------------------------------------ 汇率自动抓取

    def test_21_fx_lookup_cache_guardrails_and_failure(self) -> None:
        """/api/fx：命中缓存不重复抓、失败绝不编造、垃圾值拒绝（注入假抓取器，不打网络）。"""
        from app import fx

        calls: list[str] = []

        def fake(target: str) -> tuple[str, str, float]:
            calls.append(target)
            return "ecb", target, 7.1234

        # 1) 首次抓取并写缓存
        first = domain.fx_lookup(self.srv.store, "2026-03-01", fetcher=fake)
        self.assertEqual(first["rate"], 7.1234)
        self.assertFalse(first["cached"])
        self.assertEqual(first["source_label"], "ECB 中间价")
        self.assertEqual(first["stale_days"], 0)
        self.assertEqual(len(calls), 1)

        # 2) 二次命中缓存，不再抓取
        again = domain.fx_lookup(self.srv.store, "2026-03-01")
        self.assertTrue(again["cached"])
        self.assertEqual(again["rate"], 7.1234)
        self.assertEqual(len(calls), 1, "命中缓存不应再打网络")

        # 3) refresh 强制重抓
        forced = domain.fx_lookup(self.srv.store, "2026-03-01", fetcher=fake, refresh=True)
        self.assertFalse(forced["cached"])
        self.assertEqual(len(calls), 2)

        # 4) 失败且无更早缓存 → 502，且库里没有写入任何编造值
        def broken(target: str) -> tuple[str, str, float]:
            raise fx.FxUnavailable("两个源都不可用")

        with self.assertRaises(domain.DomainError) as ctx:
            domain.fx_lookup(self.srv.store, "2000-01-03", fetcher=broken)
        self.assertEqual(ctx.exception.status, 502)
        self.assertEqual(ctx.exception.code, "fx_unavailable")
        self.assertIsNone(self.srv.store.get_fx_rate("2000-01-03"))

        # 5) 失败但有更早缓存 → 如实标注为兜底（source_date 是更早那天）
        fallback = domain.fx_lookup(self.srv.store, "2026-03-15", fetcher=broken)
        self.assertTrue(fallback["fallback"])
        self.assertEqual(fallback["source_date"], "2026-03-01")
        self.assertGreater(fallback["stale_days"], 0)

        # 6) 周末/节假日：生效日早于请求日 → stale_days 如实标注
        weekend = domain.fx_lookup(self.srv.store, "2026-03-08",
                                   fetcher=lambda t: ("ecb", "2026-03-06", 7.2))
        self.assertEqual(weekend["source_date"], "2026-03-06")
        self.assertEqual(weekend["stale_days"], 2)

        # 7) 护栏：明显是垃圾的汇率（例如原表里出现过的笔误值）必须被拒绝
        with self.assertRaises(domain.DomainError) as bad:
            domain.fx_lookup(self.srv.store, "2026-03-20",
                             fetcher=lambda t: ("ecb", t, 999999.0))
        self.assertEqual(bad.exception.code, "fx_unavailable")

        # 8) 路由受鉴权保护
        status, _ = self.srv.get("/api/fx?date=2026-03-01")
        self.assertEqual(status, 401)

    def test_22_fx_backfill_fills_gaps_only(self) -> None:
        """回填脚本：默认只报告，--apply 只补空缺，已填写的期级汇率不动。"""
        import sqlite3

        from app import fx, fx_backfill

        db = self.tmpdir / "backfill.db"
        # 用 sqlite backup API 复制（WAL 下直接 cp 会复制到较旧状态）
        with sqlite3.connect(self.db) as src, sqlite3.connect(db) as dst:
            src.backup(dst)

        store = Store(db)
        try:
            rows = store.snapshot_fx_rows()
            missing_before = [d for d, r in rows if r is None]
            self.assertTrue(missing_before, "测试库应存在缺少汇率的期")
            kept = next((d, float(r)) for d, r in rows if r is not None)
        finally:
            store.close()

        real = fx.default_fetcher
        fx.default_fetcher = lambda target: ("ecb", target, 7.5)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = fx_backfill.main(["--db", str(db)])
            self.assertEqual(rc, 0)
            self.assertIn("空缺", out.getvalue())
            store = Store(db)
            try:
                self.assertEqual([d for d, r in store.snapshot_fx_rows() if r is None],
                                 missing_before, "默认只报告，不应写库")
            finally:
                store.close()

            with contextlib.redirect_stdout(io.StringIO()):
                rc2 = fx_backfill.main(["--db", str(db), "--apply"])
            self.assertEqual(rc2, 0)
            store = Store(db)
            try:
                after = dict(store.snapshot_fx_rows())
                self.assertEqual([d for d, r in store.snapshot_fx_rows() if r is None], [])
                self.assertEqual(after[missing_before[0]], 7.5)
                self.assertEqual(after[kept[0]], kept[1], "已填写的汇率不能被回填覆盖")
            finally:
                store.close()
        finally:
            fx.default_fetcher = real


    def test_23_carry_forward_never_suggests_a_future_date(self) -> None:
        """建议日期不得晚于今天：最后一期就是今天时，不能凭空推出「明天」的新期次。"""
        from datetime import date as _date

        today = _date.today().isoformat()
        account = self.srv.get("/api/accounts", token=self.token)[1]["accounts"][0]
        status, saved = self.srv.request("POST", "/api/snapshots", {
            "date": today,
            "items": [{"account_id": account["id"], "amount_cny": 1234.0}],
        }, token=self.token)
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["created"])
        try:
            status, cf = self.srv.request("POST", "/api/snapshots/carry-forward", {}, token=self.token)
            self.assertEqual(status, 200, cf)
            self.assertLessEqual(cf["date_default"], today, "建议日期不得晚于今天")
            self.assertEqual(cf["date_default"], today,
                             "最后一期就是今天时建议日期仍应是今天（该期已存在，前端转为编辑那一期）")
        finally:
            status, _ = self.srv.request("DELETE", f"/api/snapshots/{today}", token=self.token)
            self.assertEqual(status, 200, "清理：删除本用例新建的期次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
