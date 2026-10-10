"""信用卡额度测试（真实 SQLite + 真实 HTTP 往返，合成数据集驱动）。

数据来源：tools/seed_demo.py 在每个用例内生成合成数据集（固定随机种子），
仓库内不含任何数据文件。所有断言相对生成的数据集做结构校验，不绑定任何
固定金额/账户名/日期；额度字段通过 API 写入后再回读。

覆盖：
1. 迁移幂等（schema_version=3、列只加一次、业务计数不变）
2. 窄接口只改额度：明细行数、note、amount_cny 一字不动，净值不变；null 清回未观测
3. 校验：limit<=0 / available<0 → 400 bad_credit_info；available>limit → credit_overpaid
4. credit_mismatch：|amount| != used 时标记且金额不被改写
5. carry-forward 带出且 auto_filled=1；手改不被覆盖
6. 未观测 ≠ 零：键缺失 + reason（信用卡 not_recorded / 非信用卡 not_credit_card）
7. 净值守恒：写额度前后 metrics/networth 逐点一致
8. metrics/credit：总额/已用/使用率与逐账户一致；无观测时 absent_reason
9. export → import 保留额度
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
import sys

for extra in (BACKEND, ROOT / "tools"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import seed_demo  # noqa: E402
from app import api, config, domain  # noqa: E402
from app.store import Store  # noqa: E402

SEED = 20260101
PERIODS = 24


class ServerHandle:
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

    def request(self, method: str, path: str, body=None, token: str | None = None):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(), exc.code
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return status, payload

    def get(self, path, token=None):
        return self.request("GET", path, None, token)


def _make_synthetic_server(tmpdir: Path):
    """生成合成数据集并启动服务，返回 (handle, token, meta)。"""
    db = tmpdir / f"nw-{os.getpid()}-{id(tmpdir)}.db"
    dataset = tmpdir / "dataset"
    meta = seed_demo.generate_dataset(dataset, periods=PERIODS, seed=SEED)
    os.environ["NETWORTH_DB"] = str(db)
    os.environ["NETWORTH_SECRET"] = "unit-test-secret-credit"
    os.environ["NETWORTH_M0_OUT"] = str(dataset)
    os.environ.pop("ALLOW_REGISTRATION", None)
    from app import importer as _importer

    rc = _importer.main(["--source", str(dataset), "--db", str(db), "--quiet"])
    assert rc == 0
    srv = ServerHandle(db)
    srv.request("POST", "/api/auth/register", {"username": "credit-admin", "password": "credit-pass-123"})
    status, payload = srv.request("POST", "/api/auth/login", {"username": "credit-admin", "password": "credit-pass-123"})
    assert status == 200, payload
    token = payload["token"]
    return srv, token, meta


def _pick_credit_account(srv, token):
    """选一个信用卡账户（subclass 信用卡），若无则用任意负债账户。"""
    status, data = srv.get("/api/accounts", token=token)
    assert status == 200
    accounts = data["accounts"]
    # 优先 subclass 信用卡
    for acc in accounts:
        if acc.get("subclass") == "信用卡":
            return acc
    # 回退：选任意负债
    for acc in accounts:
        if acc.get("kind") == "liability":
            return acc
    return accounts[0]


def _pick_non_credit_liability(srv, token, credit_id):
    """选一个非信用卡负债（用于 not_credit_card 断言）。"""
    status, data = srv.get("/api/accounts", token=token)
    assert status == 200
    for acc in data["accounts"]:
        if acc["id"] == credit_id:
            continue
        if acc.get("kind") == "liability" and acc.get("subclass") != "信用卡":
            return acc
    # 兜底：任意非信用卡账户
    for acc in data["accounts"]:
        if acc["id"] != credit_id:
            return acc
    return None


class CreditTests(unittest.TestCase):
    # 1. 迁移幂等
    def test_migration_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="credit-mig-") as tmp:
            db = Path(tmp) / "old.db"
            # 构造旧库：仅有 snapshot / snapshot_item 旧结构，缺 credit 列
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE snapshot (id INTEGER PRIMARY KEY, snapshot_date TEXT UNIQUE NOT NULL, status TEXT DEFAULT 'final', default_fx_rate REAL, market_note TEXT, data_quality_flags TEXT, source TEXT DEFAULT 'manual', created_at TEXT, updated_at TEXT);
                CREATE TABLE snapshot_item (id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, account_id INTEGER NOT NULL, amount_cny REAL NOT NULL, amount_usd REAL, fx_rate REAL, note TEXT, auto_filled INTEGER DEFAULT 0, flags TEXT);
                CREATE TABLE account (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, category TEXT, subclass TEXT, kind TEXT NOT NULL CHECK(kind IN ('asset','liability')), currency TEXT DEFAULT 'CNY', is_liquid INTEGER DEFAULT 0, is_counted INTEGER DEFAULT 1, sort INTEGER DEFAULT 0, active_from TEXT, active_to TEXT, note TEXT);
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
                """
            )
            # 插入一条快照与明细用于计数校验
            conn.execute("INSERT INTO account(name, kind, subclass) VALUES (?,?,?)", ("测试信用卡", "liability", "信用卡"))
            acc_id = conn.execute("SELECT id FROM account WHERE name=?", ("测试信用卡",)).fetchone()[0]
            conn.execute("INSERT INTO snapshot(snapshot_date, status) VALUES (?,?)", ("2020-01-01", "final"))
            snap_id = conn.execute("SELECT id FROM snapshot WHERE snapshot_date=?", ("2020-01-01",)).fetchone()[0]
            conn.execute("INSERT INTO snapshot_item(snapshot_id, account_id, amount_cny) VALUES (?,?,?)", (snap_id, acc_id, -1000))
            conn.commit()
            conn.close()
            # 第一次 Store
            store = Store(db)
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(snapshot_item)").fetchall()}
            self.assertIn("credit_limit_cny", cols)
            self.assertIn("credit_available_cny", cols)
            self.assertEqual(store.get_meta("schema_version"), "3")
            counts1 = store.counts()
            store.close()
            # 第二次 Store（幂等）
            store2 = Store(db)
            cols2 = {r[1] for r in store2.conn.execute("PRAGMA table_info(snapshot_item)").fetchall()}
            self.assertIn("credit_limit_cny", cols2)
            self.assertIn("credit_available_cny", cols2)
            self.assertEqual(store2.get_meta("schema_version"), "3")
            counts2 = store2.counts()
            self.assertEqual(counts1, counts2, "重复迁移不得改变业务计数")
            # 原有明细仍存在且 credit 为 NULL
            row = store2._one("SELECT credit_limit_cny, credit_available_cny FROM snapshot_item WHERE snapshot_id=?", (snap_id,))
            self.assertIsNone(row["credit_limit_cny"])
            self.assertIsNone(row["credit_available_cny"])
            store2.close()

    # 2. 窄接口只改额度：明细不动、净值不变；null 清回未观测
    def test_credit_narrow_never_touches_items(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-narrow-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc = _pick_credit_account(srv, token)
            aid = acc["id"]
            # 另取一个普通账户用于校验行数
            status, data = srv.get("/api/accounts", token=token)
            other = [a for a in data["accounts"] if a["id"] != aid][0]
            other_id = other["id"]
            d = "2026-06-10"
            # 先建一期含两行明细
            body = {
                "date": d,
                "items": [
                    {"account_id": aid, "amount_cny": -5000, "note": "原始备注"},
                    {"account_id": other_id, "amount_cny": 1000},
                ],
            }
            st, payload = srv.request("PUT", f"/api/snapshots/{d}", body, token=token)
            self.assertEqual(st, 200, payload)
            before_items = srv.get(f"/api/snapshots/{d}", token=token)[1]["items"]
            before_networth = srv.get(f"/api/snapshots/{d}", token=token)[1]["snapshot"]["networth"]
            # 窄接口写入额度（两字段一起）
            st, got = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_limit_cny": 10000, "credit_available_cny": 4000}, token=token)
            self.assertEqual(st, 200, got)
            self.assertEqual(got["credit_limit_cny"], 10000)
            self.assertEqual(got["credit_available_cny"], 4000)
            self.assertEqual(got["credit_used_cny"], 6000)
            self.assertAlmostEqual(got["credit_utilization"], 0.6)
            # 回读：明细行数、note、amount 一字不动
            after = srv.get(f"/api/snapshots/{d}", token=token)[1]
            self.assertEqual(len(after["items"]), 2, "额度更新不得增删明细行")
            # 找到目标账户行
            target = next(i for i in after["items"] if i["account_id"] == aid)
            other_item = next(i for i in after["items"] if i["account_id"] == other_id)
            self.assertEqual(target["amount_cny"], -5000)
            self.assertEqual(target["note"], "原始备注")
            self.assertEqual(other_item["amount_cny"], 1000)
            # 净值不变
            self.assertEqual(after["snapshot"]["networth"], before_networth)
            # 只改一个字段：只给 available
            st, got2 = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_available_cny": 3000}, token=token)
            self.assertEqual(st, 200)
            self.assertEqual(got2["credit_limit_cny"], 10000, "缺键应保留原值")
            self.assertEqual(got2["credit_available_cny"], 3000)
            # null 清回未观测
            st, got3 = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_limit_cny": None, "credit_available_cny": None}, token=token)
            self.assertEqual(st, 200)
            self.assertNotIn("credit_limit_cny", got3)
            self.assertNotIn("credit_available_cny", got3)
            self.assertIn("credit_absent_reason", got3)
            # 回读也应键缺失
            after2 = srv.get(f"/api/snapshots/{d}", token=token)[1]
            target2 = next(i for i in after2["items"] if i["account_id"] == aid)
            self.assertNotIn("credit_limit_cny", target2)
            self.assertNotIn("credit_available_cny", target2)
            self.assertIn("credit_absent_reason", target2)
            # 再次确认明细仍在
            self.assertEqual(len(after2["items"]), 2)
            self.assertEqual(srv.get(f"/api/snapshots/{d}", token=token)[1]["snapshot"]["networth"], before_networth)
            # 不存在的期 / 不在该期明细的账户 → 404
            st, err = srv.request("PUT", "/api/snapshots/1999-01-01/credit/1", {"credit_limit_cny": 1000, "credit_available_cny": 500}, token=token)
            self.assertEqual(st, 404)
            st, err = srv.request("PUT", f"/api/snapshots/{d}/credit/999999", {"credit_limit_cny": 1000, "credit_available_cny": 500}, token=token)
            self.assertEqual(st, 404)
            # 整期 PUT 漏 items 仍应 400
            st, err = srv.request("PUT", f"/api/snapshots/{d}", {"date": d, "market_note": "漏 items"}, token=token)
            self.assertEqual(st, 400)
            self.assertEqual(err["error"]["code"], "partial_update_unsupported")
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 3. 校验：limit<=0 / available<0 → 400；available>limit 合法但 flags 带 credit_overpaid
    def test_credit_validation(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-validate-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc = _pick_credit_account(srv, token)
            aid = acc["id"]
            d = "2026-07-01"
            srv.request("PUT", f"/api/snapshots/{d}", {"date": d, "items": [{"account_id": aid, "amount_cny": -2000}]}, token=token)
            # limit <=0
            st, err = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_limit_cny": 0, "credit_available_cny": 0}, token=token)
            self.assertEqual(st, 400)
            self.assertEqual(err["error"]["code"], "bad_credit_info")
            st, err = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_limit_cny": -100, "credit_available_cny": 50}, token=token)
            self.assertEqual(st, 400)
            self.assertEqual(err["error"]["code"], "bad_credit_info")
            # available <0
            st, err = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_limit_cny": 10000, "credit_available_cny": -1}, token=token)
            self.assertEqual(st, 400)
            self.assertEqual(err["error"]["code"], "bad_credit_info")
            # 整期写入非法也应 400
            st, err = srv.request("PUT", f"/api/snapshots/{d}", {"date": d, "items": [{"account_id": aid, "amount_cny": -2000, "credit_limit_cny": 0, "credit_available_cny": 0}]}, token=token)
            self.assertEqual(st, 400)
            self.assertEqual(err["error"]["code"], "bad_credit_info")
            # available > limit 合法但 flags 带 credit_overpaid
            st, got = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_limit_cny": 5000, "credit_available_cny": 8000}, token=token)
            self.assertEqual(st, 200)
            self.assertIn("credit_overpaid", got.get("flags", []), "溢缴款应在 flags 里标记")
            # 回读也带
            detail = srv.get(f"/api/snapshots/{d}", token=token)[1]
            item = next(i for i in detail["items"] if i["account_id"] == aid)
            self.assertIn("credit_overpaid", item.get("flags", []))
            self.assertEqual(item["credit_limit_cny"], 5000)
            self.assertEqual(item["credit_available_cny"], 8000)
            self.assertEqual(item["credit_used_cny"], -3000)
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 4. credit_mismatch：|amount| != used 时标记且金额不被改写
    def test_credit_mismatch_flag(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-mismatch-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc = _pick_credit_account(srv, token)
            aid = acc["id"]
            d = "2026-08-01"
            # amount -5000，额度 10000-4000=6000 → | -5000 | != 6000 → mismatch
            body = {
                "date": d,
                "items": [
                    {"account_id": aid, "amount_cny": -5000, "credit_limit_cny": 10000, "credit_available_cny": 4000}
                ],
            }
            st, saved = srv.request("PUT", f"/api/snapshots/{d}", body, token=token)
            self.assertEqual(st, 200, saved)
            detail = srv.get(f"/api/snapshots/{d}", token=token)[1]
            item = next(i for i in detail["items"] if i["account_id"] == aid)
            self.assertTrue(item.get("credit_mismatch"), "应标记 mismatch")
            self.assertEqual(item["amount_cny"], -5000, "金额不得被额度改写")
            self.assertEqual(item["credit_used_cny"], 6000)
            # 构造一致的：amount -6000，used 6000 → 无 mismatch
            d2 = "2026-08-02"
            body2 = {
                "date": d2,
                "items": [
                    {"account_id": aid, "amount_cny": -6000, "credit_limit_cny": 10000, "credit_available_cny": 4000}
                ],
            }
            srv.request("PUT", f"/api/snapshots/{d2}", body2, token=token)
            item2 = next(i for i in srv.get(f"/api/snapshots/{d2}", token=token)[1]["items"] if i["account_id"] == aid)
            self.assertNotIn("credit_mismatch", item2, "一致时不应标记 mismatch")
            # 窄接口写入导致 mismatch 也应标记
            d3 = "2026-08-03"
            srv.request("PUT", f"/api/snapshots/{d3}", {"date": d3, "items": [{"account_id": aid, "amount_cny": -1000}]}, token=token)
            st, got = srv.request("PUT", f"/api/snapshots/{d3}/credit/{aid}", {"credit_limit_cny": 20000, "credit_available_cny": 5000}, token=token)
            self.assertEqual(st, 200)
            self.assertTrue(got.get("credit_mismatch"))
            # 金额仍 -1000
            self.assertEqual(got["amount_cny"], -1000)
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 5. carry-forward 带出且 auto_filled=1；手改不被覆盖
    def test_carry_forward_credit(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-cf-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc = _pick_credit_account(srv, token)
            aid = acc["id"]
            # 创建带额度的期 A
            d1 = "2026-09-01"
            srv.request("PUT", f"/api/snapshots/{d1}", {"date": d1, "items": [{"account_id": aid, "amount_cny": -1000, "credit_limit_cny": 10000, "credit_available_cny": 7000}]}, token=token)
            # carry-forward 从 d1 → 应带出额度且 auto_filled=1
            st, cf = srv.request("POST", "/api/snapshots/carry-forward", {"from": d1}, token=token)
            self.assertEqual(st, 200)
            self.assertIn("items", cf)
            cf_item = next(i for i in cf["items"] if i["account_id"] == aid)
            self.assertEqual(cf_item["credit_limit_cny"], 10000)
            self.assertEqual(cf_item["credit_available_cny"], 7000)
            self.assertEqual(cf_item["credit_used_cny"], 3000)
            self.assertEqual(cf_item["auto_filled"], 1)
            # 用 carry-forward 的 items 存新期 B
            d2 = "2026-10-01"
            # cf 的 items 已包含额度，直接保存
            payload = {"date": d2, "items": [{"account_id": aid, "amount_cny": -1200, "credit_limit_cny": cf_item["credit_limit_cny"], "credit_available_cny": cf_item["credit_available_cny"], "auto_filled": cf_item["auto_filled"]}]}
            st, saved = srv.request("PUT", f"/api/snapshots/{d2}", payload, token=token)
            self.assertEqual(st, 200, saved)
            # 手改 B 的额度
            st, edited = srv.request("PUT", f"/api/snapshots/{d2}/credit/{aid}", {"credit_limit_cny": 20000, "credit_available_cny": 5000}, token=token)
            self.assertEqual(st, 200)
            self.assertEqual(edited["credit_limit_cny"], 20000)
            # 再次 fetch B，应保留手改值，不被自动带入覆盖
            detail = srv.get(f"/api/snapshots/{d2}", token=token)[1]
            item_b = next(i for i in detail["items"] if i["account_id"] == aid)
            self.assertEqual(item_b["credit_limit_cny"], 20000)
            self.assertEqual(item_b["credit_available_cny"], 5000)
            # 再次 carry-forward 从 d1，仍是 d1 的值，不应把 B 的手改值反向带回
            st, cf2 = srv.request("POST", "/api/snapshots/carry-forward", {"from": d1}, token=token)
            cf_item2 = next(i for i in cf2["items"] if i["account_id"] == aid)
            self.assertEqual(cf_item2["credit_limit_cny"], 10000)
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 6. 未观测 ≠ 零：键缺失 + reason
    def test_unobserved_not_zero(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-unobs-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc_credit = _pick_credit_account(srv, token)
            aid_credit = acc_credit["id"]
            acc_non = _pick_non_credit_liability(srv, token, aid_credit)
            # 选一个合成期（seed 数据的期）不带额度，断言未观测语义
            pick_date = meta["dates"][0]
            status, detail = srv.get(f"/api/snapshots/{pick_date}", token=token)
            self.assertEqual(status, 200)
            # 找到信信用卡与非信用卡的明细
            for item in detail["items"]:
                # 未观测时两者键缺失
                if "credit_limit_cny" in item or "credit_available_cny" in item:
                    continue  # 该行有额度，跳过
                self.assertNotIn("credit_used_cny", item)
                self.assertNotIn("credit_utilization", item)
                self.assertIn("credit_absent_reason", item)
                if item["subclass"] == "信用卡":
                    self.assertEqual(item["credit_absent_reason"], "not_recorded")
                else:
                    # 非信用卡负债应为 not_credit_card（但也可能是非负债）
                    if item["kind"] == "liability" and item["subclass"] != "信用卡":
                        self.assertEqual(item["credit_absent_reason"], "not_credit_card")
            # 额外建一期：信用卡但未记录 vs 非信用卡
            if acc_non is not None:
                d = "2026-11-01"
                srv.request("PUT", f"/api/snapshots/{d}", {"date": d, "items": [{"account_id": aid_credit, "amount_cny": -1000}, {"account_id": acc_non["id"], "amount_cny": -2000}]}, token=token)
                status, detail = srv.get(f"/api/snapshots/{d}", token=token)
                item_cc = next(i for i in detail["items"] if i["account_id"] == aid_credit)
                item_nc = next(i for i in detail["items"] if i["account_id"] == acc_non["id"])
                self.assertNotIn("credit_limit_cny", item_cc)
                self.assertEqual(item_cc["credit_absent_reason"], "not_recorded", "信用卡未记录应为 not_recorded")
                self.assertNotIn("credit_limit_cny", item_nc)
                self.assertEqual(item_nc["credit_absent_reason"], "not_credit_card")
            # accounts/series 对未观测期键缺失
            status, series = srv.get(f"/api/accounts/{aid_credit}/series", token=token)
            self.assertEqual(status, 200)
            for pt in series["points"]:
                if "credit_limit_cny" not in pt:
                    self.assertNotIn("credit_used_cny", pt)
                    self.assertNotIn("credit_utilization", pt)
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 7. 净值守恒：写额度前后 metrics/networth 逐点一致
    def test_networth_conserved(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-networth-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc = _pick_credit_account(srv, token)
            aid = acc["id"]
            # 取一次 networth
            status, before = srv.get("/api/metrics/networth", token=token)
            self.assertEqual(status, 200)
            points_before = before["points"]
            # 新建一期并反复改额度
            d = "2026-12-01"
            srv.request("PUT", f"/api/snapshots/{d}", {"date": d, "items": [{"account_id": aid, "amount_cny": -3000}]}, token=token)
            status, mid = srv.get("/api/metrics/networth", token=token)
            points_mid = mid["points"]
            # 写额度
            srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_limit_cny": 10000, "credit_available_cny": 2000}, token=token)
            status, after = srv.get("/api/metrics/networth", token=token)
            points_after = after["points"]
            # 额度字段永不参与净值：mid 与 after 的点应完全一致
            self.assertEqual(len(points_mid), len(points_after))
            for a, b in zip(points_mid, points_after):
                self.assertEqual(a["networth"], b["networth"])
                self.assertEqual(a["total_assets"], b["total_assets"])
                self.assertEqual(a["total_liabilities"], b["total_liabilities"])
            # 再改一次（溢缴款）仍一致
            srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}", {"credit_available_cny": 15000}, token=token)
            status, after2 = srv.get("/api/metrics/networth", token=token)
            for a, b in zip(points_after, after2["points"]):
                self.assertEqual(a["networth"], b["networth"])
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 8. metrics/credit：总额/已用/使用率与逐账户一致；无观测时 absent_reason
    def test_metrics_credit(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-metrics-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            # 初始无观测 → absent_reason
            status, mc = srv.get("/api/metrics/credit", token=token)
            self.assertEqual(status, 200)
            # 最新一期可能是合成数据的最后一期，尚无额度，故应有 absent_reason
            self.assertIn("absent_reason", mc)
            self.assertEqual(mc["absent_reason"], "not_recorded")
            self.assertEqual(mc["total_limit"], 0)
            self.assertIsNone(mc["utilization"])
            # 准备：在最新一期写入额度
            accounts = srv.get("/api/accounts", token=token)[1]["accounts"]
            credit_accs = [a for a in accounts if a.get("subclass") == "信用卡"]
            if len(credit_accs) < 2:
                # 若合成数据只有 3 张信用卡，取前 2
                credit_accs = [a for a in accounts if a.get("kind") == "liability"][:2]
            # 用最新日期额外建一期并写入额度（保证是最新一期）
            latest = srv.get("/api/snapshots", token=token)[1]["snapshots"][-1]["date"]
            # 取合成数据最后一期的下一月作为最新
            new_date = "2026-12-15"
            amounts = [-4000, -6000]
            limits = [10000, 20000]
            avails = [6000, 5000]  # used 4000,15000
            items = []
            for acc, amt, lim, avail in zip(credit_accs[:2], amounts, limits, avails):
                items.append({"account_id": acc["id"], "amount_cny": amt, "credit_limit_cny": lim, "credit_available_cny": avail})
            # 若账户不足，补一个非信用卡负债不计入 totals
            st, saved = srv.request("PUT", f"/api/snapshots/{new_date}", {"date": new_date, "items": items}, token=token)
            self.assertEqual(st, 200, saved)
            status, mc2 = srv.get("/api/metrics/credit", token=token)
            self.assertEqual(status, 200)
            self.assertEqual(mc2["date"], new_date)
            self.assertNotIn("absent_reason", mc2, "有观测时不应有 absent_reason")
            expected_limit = sum(limits)
            expected_used = sum(l - a for l, a in zip(limits, avails))
            expected_avail = sum(avails)
            self.assertEqual(mc2["total_limit"], expected_limit)
            self.assertEqual(mc2["total_used"], expected_used)
            self.assertEqual(mc2["total_available"], expected_avail)
            self.assertAlmostEqual(mc2["utilization"], expected_used / expected_limit)
            # by_account 一致
            self.assertEqual(len(mc2["by_account"]), len(credit_accs[:2]))
            for entry in mc2["by_account"]:
                # 每个 entry 的 used = limit - available
                self.assertEqual(entry["used"], entry["limit"] - entry["available"])
                self.assertAlmostEqual(entry["utilization"], entry["used"] / entry["limit"] if entry["limit"] else 0)
            # 未记录账户列表应包含其它信用卡（若有）
            # 至少校验 unrecorded_accounts 存在
            self.assertIn("unrecorded_accounts", mc2)
            # total_* 只累加有观测的账户
            # 再写一个溢缴款并校验 totals 包含负 used
            # 已在上面校验 totals 逻辑
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 9. export → import 保留额度
    def test_export_import_retains_credit(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-export-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc = _pick_credit_account(srv, token)
            aid = acc["id"]
            d = "2026-04-04"
            srv.request("PUT", f"/api/snapshots/{d}", {"date": d, "items": [{"account_id": aid, "amount_cny": -1234, "credit_limit_cny": 50000, "credit_available_cny": 30000}]}, token=token)
            # export
            status, exported = srv.get("/api/export", token=token)
            self.assertEqual(status, 200)
            # 找到该期
            snap = next(s for s in exported["snapshots"] if s["date"] == d)
            item = next(i for i in snap["items"] if i["account_id"] == aid)
            self.assertEqual(item["credit_limit_cny"], 50000)
            self.assertEqual(item["credit_available_cny"], 30000)
            # import 到新库
            with tempfile.TemporaryDirectory(prefix="credit-import-") as tmp2:
                db2 = Path(tmp2) / "import.db"
                store2 = Store(db2)
                # 模拟 import_payload 直接导入（不经过 HTTP）
                # 需要加载配置但直接用 domain
                result = domain.import_payload(store2, exported)
                self.assertTrue(result["ok"])
                # 验证新库中额度保留
                # 新库的 snapshot 明细
                snap2 = store2.get_snapshot(d)
                self.assertIsNotNone(snap2)
                items2 = store2.items_of(int(snap2["id"]))
                credit_item = next(i for i in items2 if int(i["account_id"]) == aid)
                self.assertEqual(float(credit_item["credit_limit_cny"]), 50000)
                self.assertEqual(float(credit_item["credit_available_cny"]), 30000)
                # 向后兼容：不带额度的导出再导入应为 NULL
                # 构造一个无额度快照的导出
                simple_export = {
                    "version": 1,
                    "accounts": [{"name": acc["name"], "kind": acc["kind"], "category": acc["category"], "subclass": acc["subclass"]}],
                    "snapshots": [{"date": "2026-05-05", "items": [{"account": acc["name"], "amount_cny": -100}] }],
                    "events": [],
                }
                result2 = domain.import_payload(store2, simple_export)
                self.assertTrue(result2["ok"])
                snap3 = store2.get_snapshot("2026-05-05")
                items3 = store2.items_of(int(snap3["id"]))
                self.assertIsNone(items3[0]["credit_limit_cny"])
                self.assertIsNone(items3[0]["credit_available_cny"])
                store2.close()
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 额外：GET /api/accounts 最新额度与 series 键缺失语义
    def test_accounts_and_series_credit_keys(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-acc-series-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc = _pick_credit_account(srv, token)
            aid = acc["id"]
            d = "2026-03-03"
            srv.request("PUT", f"/api/snapshots/{d}", {"date": d, "items": [{"account_id": aid, "amount_cny": -2000, "credit_limit_cny": 8000, "credit_available_cny": 5000}]}, token=token)
            # accounts 最新额度
            status, accs = srv.get("/api/accounts", token=token)
            self.assertEqual(status, 200)
            my = next(a for a in accs["accounts"] if a["id"] == aid)
            self.assertTrue(my.get("is_credit_card"))
            self.assertEqual(my["credit_limit_cny"], 8000)
            self.assertEqual(my["credit_available_cny"], 5000)
            self.assertEqual(my["credit_used_cny"], 3000)
            self.assertAlmostEqual(my["credit_utilization"], 0.375)
            # series 的 point 应带额度
            status, series = srv.get(f"/api/accounts/{aid}/series", token=token)
            self.assertEqual(status, 200)
            pt = next(p for p in series["points"] if p["date"] == d)
            self.assertEqual(pt["credit_limit_cny"], 8000)
            self.assertEqual(pt["credit_available_cny"], 5000)
            # 未观测期键缺失
            unobs = [p for p in series["points"] if p["date"] != d and "credit_limit_cny" not in p]
            # 至少存在一个未观测点且不插 0
            self.assertGreaterEqual(len(unobs), 1)
            for p in unobs:
                self.assertNotIn("credit_limit_cny", p)
                self.assertNotIn("credit_available_cny", p)
                self.assertNotIn("credit_used_cny", p)
                self.assertNotIn("credit_utilization", p)
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 11. 只记了一半（只填总额度）时，已记的值必须可见、派生不编
    #     —— 用户几乎必然先填总额度，若把整行判成「未观测」，保存后回读就看不到自己填的值
    def test_partial_observation_is_visible(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="credit-partial-"))
        srv, token, meta = _make_synthetic_server(tmpdir)
        try:
            acc = _pick_credit_account(srv, token)
            aid = acc["id"]
            d = "2026-05-05"
            srv.request("PUT", f"/api/snapshots/{d}", {"date": d, "items": [{"account_id": aid, "amount_cny": -3000}]}, token=token)

            # 窄接口只写总额度
            st, got = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}",
                                  {"credit_limit_cny": 30000}, token=token)
            self.assertEqual(st, 200, got)
            self.assertEqual(got["credit_limit_cny"], 30000, "已填的总额度必须回读得到")
            self.assertNotIn("credit_available_cny", got, "未观测的剩余额度不应出现")
            self.assertNotIn("credit_used_cny", got, "派生值要求两侧都在")
            self.assertNotIn("credit_utilization", got)
            self.assertEqual(got["credit_absent_reason"], "not_recorded", "如实说明仍有未记录的部分")

            # 整期详情
            item = next(i for i in srv.get(f"/api/snapshots/{d}", token=token)[1]["items"]
                        if i["account_id"] == aid)
            self.assertEqual(item["credit_limit_cny"], 30000)
            self.assertNotIn("credit_available_cny", item)

            # 账户列表（最后一期往往是未观测的，必须回溯到最近一个有观测的期）
            my = next(a for a in srv.get("/api/accounts", token=token)[1]["accounts"] if a["id"] == aid)
            self.assertTrue(my.get("is_credit_card"))
            self.assertEqual(my.get("credit_limit_cny"), 30000)
            self.assertIsNone(my.get("credit_available_cny"))
            self.assertIsNone(my.get("credit_used_cny"))

            # series 的该点带 limit、不带派生
            pt = next(p for p in srv.get(f"/api/accounts/{aid}/series", token=token)[1]["points"]
                      if p["date"] == d)
            self.assertEqual(pt["credit_limit_cny"], 30000)
            self.assertNotIn("credit_used_cny", pt)

            # metrics：只记一侧也要进 by_account，且整体使用率不编
            mc = srv.get("/api/metrics/credit", token=token)[1]
            row = next(r for r in mc["by_account"] if r["account_id"] == aid)
            self.assertEqual(row["limit"], 30000)
            self.assertIsNone(row["available"])
            self.assertIsNone(row["used"])
            self.assertEqual(mc["total_limit"], 30000)
            self.assertIsNone(mc["utilization"], "缺剩余额度时整体使用率不能编")
            self.assertTrue(all(x["account_id"] != aid for x in mc["unrecorded_accounts"]),
                            "只记一侧的账户不算完全未观测")

            # 补上剩余额度后派生出齐
            st, both = srv.request("PUT", f"/api/snapshots/{d}/credit/{aid}",
                                   {"credit_available_cny": 12000}, token=token)
            self.assertEqual(st, 200, both)
            self.assertEqual(both["credit_limit_cny"], 30000)
            self.assertEqual(both["credit_used_cny"], 18000)
            self.assertNotIn("credit_absent_reason", both)
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
