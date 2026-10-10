"""地点一等字段测试（真实 SQLite + 真实 HTTP 往返，合成数据集驱动）。

数据来源：tools/seed_demo.py 在每个用例内生成合成数据集（固定随机种子），
仓库内不含任何数据文件。所有断言相对生成的数据集做结构校验，不绑定任何
固定金额/账户名/日期；地点字段通过 API 写入后再回读（数据集本身不含 location）。

覆盖：
1. 迁移幂等
2. carry-forward 取最近且有地点的期
3. 保存显式 location 后回读
4. source 白名单
5. reverse：off/缓存/失败
6. 未观测期键缺失
7. metrics/locations 聚合
8. backfill dry-run/apply/冲突/幂等/不改 note
9. 锚点不破（服务端重算 == 明细合计 + 期数守恒）
额外：
0. 地点专用窄接口：只改地点、明细一字不动；整期 PUT 漏 items 必须被拒绝
0b. 逆地理编码落到城市级（display_name 长串不能直接当地点）
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
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
    """在 tmpdir 下生成合成数据集并导入，启动临时服务，返回 (handle, token, meta, dataset_dir)。"""
    db = tmpdir / f"nw-{os.getpid()}-{id(tmpdir)}.db"
    dataset = tmpdir / "dataset"
    meta = seed_demo.generate_dataset(dataset, periods=PERIODS, seed=SEED)
    os.environ["NETWORTH_DB"] = str(db)
    os.environ["NETWORTH_SECRET"] = "unit-test-secret-loc"
    os.environ["NETWORTH_M0_OUT"] = str(dataset)
    os.environ.pop("ALLOW_REGISTRATION", None)
    from app import importer as _importer

    rc = _importer.main(["--source", str(dataset), "--db", str(db), "--quiet"])
    assert rc == 0
    srv = ServerHandle(db)
    srv.request("POST", "/api/auth/register", {"username": "loc-admin", "password": "loc-pass-123"})
    status, payload = srv.request("POST", "/api/auth/login", {"username": "loc-admin", "password": "loc-pass-123"})
    assert status == 200, payload
    token = payload["token"]
    return srv, token, meta, dataset


class LocationTests(unittest.TestCase):

    # 0. 地点专用窄接口：只改地点、明细一字不动；整期 PUT 漏 items 必须被拒绝（防静默清空）
    def test_location_dedicated_write_never_touches_items(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-set-"))
        srv, token, meta, _ = _make_synthetic_server(tmpdir)
        try:
            accounts = srv.get("/api/accounts", token=token)[1]["accounts"]
            a1, a2 = accounts[0]["id"], accounts[1]["id"]
            d = "2026-06-10"
            body = {
                "date": d,
                "items": [{"account_id": a1, "amount_cny": 1234.5, "note": "原始备注"},
                          {"account_id": a2, "amount_cny": -200}],
                "location": {"text": "SynthCityA", "source": "manual"},
            }
            st, payload = srv.request("PUT", f"/api/snapshots/{d}", body, token=token)
            self.assertEqual(st, 200, payload)
            before = srv.get(f"/api/snapshots/{d}", token=token)[1]["snapshot"]
            self.assertEqual(len(srv.get(f"/api/snapshots/{d}", token=token)[1]["items"]), 2)

            # 专用接口改地点
            st, payload = srv.request("PUT", f"/api/snapshots/{d}/location",
                                      {"location": {"text": "SynthCityB", "source": "manual",
                                                    "observed_at": "2026-06-10T12:00:00+08:00"}}, token=token)
            self.assertEqual(st, 200, payload)
            after = srv.get(f"/api/snapshots/{d}", token=token)[1]
            self.assertEqual(after["snapshot"]["location"]["text"], "SynthCityB")
            self.assertEqual(after["snapshot"]["location"]["source"], "manual")
            self.assertEqual(after["snapshot"]["networth"], before["networth"])
            self.assertEqual(len(after["items"]), 2, "地点更新不得触碰明细")
            self.assertEqual({i["account_id"] for i in after["items"]}, {a1, a2})
            self.assertEqual([i["note"] for i in after["items"] if i["account_id"] == a1][0], "原始备注")

            # 平铺形态 body 也接受
            st, _ = srv.request("POST", f"/api/snapshots/{d}/location",
                                {"text": "SynthCityC", "source": "manual"}, token=token)
            self.assertEqual(st, 200)
            self.assertEqual(srv.get(f"/api/snapshots/{d}", token=token)[1]["snapshot"]["location"]["text"], "SynthCityC")

            # 清空 → 回到未观测（键缺失 + reason），明细仍在
            st, _ = srv.request("POST", f"/api/snapshots/{d}/location", {"location": None}, token=token)
            self.assertEqual(st, 200)
            cleared = srv.get(f"/api/snapshots/{d}", token=token)[1]
            self.assertNotIn("location", cleared["snapshot"])
            self.assertIn("location_absent_reason", cleared["snapshot"])
            self.assertEqual(len(cleared["items"]), 2)

            # parsed_note 仍被 API 拒绝；不存在的期 → 404
            st, err = srv.request("PUT", f"/api/snapshots/{d}/location",
                                  {"location": {"text": "X", "source": "parsed_note"}}, token=token)
            self.assertEqual(st, 400, err)
            self.assertEqual(err["error"]["code"], "bad_location")
            st, _ = srv.request("PUT", "/api/snapshots/1999-01-01/location",
                                {"location": {"text": "X", "source": "manual"}}, token=token)
            self.assertEqual(st, 404)

            # 防静默清空：更新已存在的期但不带 items 键 → 400，且明细原样保留
            st, err = srv.request("PUT", f"/api/snapshots/{d}",
                                  {"date": d, "location": {"text": "SynthCityD", "source": "manual"}}, token=token)
            self.assertEqual(st, 400, err)
            self.assertEqual(err["error"]["code"], "partial_update_unsupported")
            kept = srv.get(f"/api/snapshots/{d}", token=token)[1]
            self.assertEqual(len(kept["items"]), 2, "被拒绝的请求不得留下任何写入痕迹")
            self.assertNotIn("location", kept["snapshot"])
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 0b. 逆地理编码落到城市级（display_name 是街道长长串，不能直接当地点）
    #     用例全部使用合成地名：真实城市名属于个人数据，不得写进仓库。
    def test_nominatim_returns_city_level(self):
        from app import geocode
        orig = geocode._http_get
        try:
            # 情况1：address 里 city/state 就是市名 → 取市名
            geocode._http_get = lambda url, headers=None, timeout=8: json.dumps(
                {"display_name": "示例街道, 示例核心区, 示例区, 示例市, 100000, 中国",
                 "address": {"suburb": "示例街道", "city": "示例市", "state": "示例市", "country": "中国"}},
                ensure_ascii=False)
            text, raw = geocode.nominatim_fetch(1.0, 2.0)
            self.assertEqual(text, "示例", "市名应取到并去掉后缀「市」")
            self.assertIn("示例街道", raw, "原始响应要保留在 raw 里，不丢证据")
            # 情况2：地级市形态（state 是省，市名在 city）
            geocode._http_get = lambda url, headers=None, timeout=8: json.dumps(
                {"display_name": "示例街道, 示例区, 示例市, 示例省, 中国",
                 "address": {"suburb": "示例街道", "city": "示例市", "state": "示例省", "country": "中国"}},
                ensure_ascii=False)
            self.assertEqual(geocode.nominatim_fetch(1.0, 2.0)[0], "示例")
            # 情况3：address 里没有市名（city 是区）→ 从 display_name 的「市」段取
            geocode._http_get = lambda url, headers=None, timeout=8: json.dumps(
                {"display_name": "示例街道, 示例核心区, 示例区, 示例市, 100000, 中国",
                 "address": {"suburb": "示例街道", "city": "示例区", "postcode": "100000", "country": "中国"}},
                ensure_ascii=False)
            self.assertEqual(geocode.nominatim_fetch(1.0, 2.0)[0], "示例")
            # 情况4：city 是区、state 是省 → 仍从 display_name 取到市名
            geocode._http_get = lambda url, headers=None, timeout=8: json.dumps(
                {"display_name": "示例街道, 示例区, 示例市, 示例省, 100000, 中国",
                 "address": {"suburb": "示例街道", "city": "示例区", "state": "示例省", "country": "中国"}},
                ensure_ascii=False)
            self.assertEqual(geocode.nominatim_fetch(1.0, 2.0)[0], "示例")
            # 情况5：只有区级（无市名、无 display_name）时如实返回区，不编造市名
            geocode._http_get = lambda url, headers=None, timeout=8: json.dumps(
                {"address": {"city": "示例区", "state": "示例省"}}, ensure_ascii=False)
            self.assertEqual(geocode.nominatim_fetch(1.0, 2.0)[0], "示例区")
            # 情况6：无 address 组件 → 回退 display_name 首段（仍不是整串）
            geocode._http_get = lambda url, headers=None, timeout=8: json.dumps(
                {"display_name": "Somewhere, Region, Country"}, ensure_ascii=False)
            self.assertEqual(geocode.nominatim_fetch(1.0, 2.0)[0], "Somewhere")
            # 情况7：两者都缺 → 明确失败，绝不编造
            geocode._http_get = lambda url, headers=None, timeout=8: json.dumps({"display_name": ""}, ensure_ascii=False)
            with self.assertRaises(geocode.GeocodeUnavailable):
                geocode.nominatim_fetch(1.0, 2.0)
        finally:
            geocode._http_get = orig

    # 1. 迁移幂等
    def test_migration_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="loc-mig-") as tmp:
            db = Path(tmp) / "old.db"
            # 构造旧 schema（缺 location 列）
            conn = sqlite3.connect(db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
            CREATE TABLE snapshot (id INTEGER PRIMARY KEY, snapshot_date TEXT UNIQUE NOT NULL, status TEXT DEFAULT 'final', default_fx_rate REAL, market_note TEXT, data_quality_flags TEXT, source TEXT DEFAULT 'manual', created_at TEXT, updated_at TEXT);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            """)
            conn.execute("INSERT INTO snapshot(snapshot_date, status) VALUES (?,?)", ("2020-01-01", "final"))
            conn.commit()
            conn.close()
            # 第一次 init
            store = Store(db)
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(snapshot)").fetchall()}
            for col in ("location_text", "location_source", "location_observed_at", "location_lat", "location_lon", "location_accuracy_m"):
                self.assertIn(col, cols, f"迁移后应存在列 {col}")
            # 既有数据保留
            row = store.get_snapshot("2020-01-01")
            self.assertIsNotNone(row)
            self.assertEqual(row["snapshot_date"], "2020-01-01")
            # schema_version
            self.assertEqual(store.get_meta("schema_version"), "2")
            store.close()
            # 第二次 init（幂等）
            store2 = Store(db)
            cols2 = {r[1] for r in store2.conn.execute("PRAGMA table_info(snapshot)").fetchall()}
            for col in ("location_text", "location_source", "location_observed_at", "location_lat", "location_lon", "location_accuracy_m"):
                self.assertIn(col, cols2)
            row2 = store2.get_snapshot("2020-01-01")
            self.assertIsNotNone(row2)
            self.assertEqual(store2.get_meta("schema_version"), "2")
            # geocode_cache 存在
            tbl = store2.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='geocode_cache'").fetchone()
            self.assertIsNotNone(tbl)
            store2.close()

    # 2. carry-forward 取最近且有地点的期
    def test_carry_forward_location(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-cf-"))
        srv, token, meta, _ = _make_synthetic_server(tmpdir)
        try:
            accounts = srv.get("/api/accounts", token=token)[1]["accounts"]
            aid = accounts[0]["id"]
            # 创建带地点的期：2026-01-15 SynthCityA, 2026-02-15 SynthCityB, 2026-04-15 SynthCityC；2026-03-15 无地点
            for d, loc in [("2026-01-15", "SynthCityA"), ("2026-02-15", "SynthCityB"), ("2026-04-15", "SynthCityC")]:
                body = {"date": d, "items": [{"account_id": aid, "amount_cny": 1000}], "location": {"text": loc, "source": "manual"}}
                status, payload = srv.request("PUT", f"/api/snapshots/{d}", body, token=token)
                self.assertEqual(status, 200, payload)
            # 再建一个无地点的期 2026-03-15
            status, _ = srv.request("PUT", "/api/snapshots/2026-03-15", {"date": "2026-03-15", "items": [{"account_id": aid, "amount_cny": 1000}]}, token=token)
            self.assertEqual(status, 200)
            # carry-forward with from → 应取 2026-04-15（严格早于建议日期的最近有地点）
            status, cf = srv.request("POST", "/api/snapshots/carry-forward", {"from": "2026-04-15"}, token=token)
            self.assertEqual(status, 200)
            self.assertIn("location_default", cf)
            self.assertEqual(cf["location_default"]["text"], "SynthCityC")
            self.assertEqual(cf["location_default"]["source"], "carried")
            self.assertEqual(cf["location_default"]["from_date"], "2026-04-15")
            # 无则键缺失：空库场景
            with tempfile.TemporaryDirectory(prefix="loc-empty-") as tmp2:
                db2 = Path(tmp2) / "empty.db"
                store2 = Store(db2)
                srv2 = ServerHandle(db2)
                try:
                    srv2.request("POST", "/api/auth/register", {"username": "u2", "password": "p123456"})
                    _, sess = srv2.request("POST", "/api/auth/login", {"username": "u2", "password": "p123456"})
                    tok2 = sess["token"]
                    status, cf2 = srv2.request("POST", "/api/snapshots/carry-forward", {}, token=tok2)
                    self.assertEqual(status, 200)
                    self.assertNotIn("location_default", cf2)
                    self.assertIn("location_absent_reason", cf2)
                finally:
                    srv2.close()
                    store2.close()
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 3. 保存显式 location 后回读一致；再次 carry-forward 返回 carried 且不覆盖
    def test_save_and_read_location(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-save-"))
        srv, token, meta, _ = _make_synthetic_server(tmpdir)
        try:
            accounts = srv.get("/api/accounts", token=token)[1]["accounts"]
            aid = accounts[0]["id"]
            date = "2026-06-15"
            loc = {"text": "SynthCityX", "source": "manual", "observed_at": "2026-06-15T12:00:00Z", "lat": 30.5, "lon": 120.1, "accuracy_m": 50}
            body = {"date": date, "items": [{"account_id": aid, "amount_cny": 2000}], "location": loc}
            status, saved = srv.request("PUT", f"/api/snapshots/{date}", body, token=token)
            self.assertEqual(status, 200, saved)
            # 回读
            status, detail = srv.get(f"/api/snapshots/{date}", token=token)
            self.assertEqual(status, 200)
            self.assertIn("location", detail["snapshot"])
            self.assertEqual(detail["snapshot"]["location"]["text"], "SynthCityX")
            self.assertEqual(detail["snapshot"]["location"]["source"], "manual")
            self.assertAlmostEqual(detail["snapshot"]["location"]["lat"], 30.5)
            self.assertAlmostEqual(detail["snapshot"]["location"]["lon"], 120.1)
            self.assertAlmostEqual(detail["snapshot"]["location"]["accuracy_m"], 50)
            # 再次用 PUT 不带 location 更新金额，不应覆盖已保存的地点
            body2 = {"date": date, "items": [{"account_id": aid, "amount_cny": 3000}]}
            status, saved2 = srv.request("PUT", f"/api/snapshots/{date}", body2, token=token)
            self.assertEqual(status, 200, saved2)
            status, detail2 = srv.get(f"/api/snapshots/{date}", token=token)
            self.assertEqual(detail2["snapshot"]["location"]["text"], "SynthCityX", "自动带入永不覆盖已手动改过的值")
            # 列表也带
            status, lst = srv.get("/api/snapshots", token=token)
            row = next(r for r in lst["snapshots"] if r["date"] == date)
            self.assertIn("location", row)
            self.assertEqual(row["location"]["text"], "SynthCityX")
            # carry-forward 应返回 carried
            status, cf = srv.request("POST", "/api/snapshots/carry-forward", {"from": date}, token=token)
            self.assertEqual(status, 200)
            self.assertIn("location_default", cf)
            self.assertEqual(cf["location_default"]["source"], "carried")
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 4. source 白名单
    def test_location_source_validation(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-validate-"))
        srv, token, meta, _ = _make_synthetic_server(tmpdir)
        try:
            accounts = srv.get("/api/accounts", token=token)[1]["accounts"]
            aid = accounts[0]["id"]
            # 非法 source
            status, payload = srv.request("PUT", "/api/snapshots/2026-07-01", {"date": "2026-07-01", "items": [{"account_id": aid, "amount_cny": 1000}], "location": {"text": "Foo", "source": "invalid"}}, token=token)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "bad_location")
            # device 缺 lat/lon
            status, payload = srv.request("PUT", "/api/snapshots/2026-07-02", {"date": "2026-07-02", "items": [{"account_id": aid, "amount_cny": 1000}], "location": {"text": "Foo", "source": "device"}}, token=token)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "bad_location")
            # parsed_note 经 API 拒绝
            status, payload = srv.request("PUT", "/api/snapshots/2026-07-03", {"date": "2026-07-03", "items": [{"account_id": aid, "amount_cny": 1000}], "location": {"text": "Foo", "source": "parsed_note"}}, token=token)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "bad_location")
            # lat 超范围
            status, payload = srv.request("PUT", "/api/snapshots/2026-07-04", {"date": "2026-07-04", "items": [{"account_id": aid, "amount_cny": 1000}], "location": {"text": "Foo", "source": "manual", "lat": 100, "lon": 0}}, token=token)
            self.assertEqual(status, 400)
            # text 空
            status, payload = srv.request("PUT", "/api/snapshots/2026-07-05", {"date": "2026-07-05", "items": [{"account_id": aid, "amount_cny": 1000}], "location": {"text": "   ", "source": "manual"}}, token=token)
            self.assertEqual(status, 400)
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 5. reverse
    def test_reverse(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-reverse-"))
        srv, token, meta, _ = _make_synthetic_server(tmpdir)
        try:
            # provider=off 默认 -> 409
            os.environ["NETWORTH_GEOCODE_PROVIDER"] = "off"
            status, payload = srv.get("/api/location/reverse?lat=30&lon=120", token=token)
            self.assertEqual(status, 409)
            self.assertEqual(payload["error"]["code"], "provider_disabled")
            # 用注入 fake fetcher 成功 -> 通过 domain 层直接测试缓存与单次调用
            from app import geocode as _geocode
            calls = []

            def fake(lat, lon):
                calls.append((lat, lon))
                return "FakeCity", '{"display_name":"FakeCity"}'

            os.environ["NETWORTH_GEOCODE_PROVIDER"] = "nominatim"
            # 第一次应走 fetcher
            res1 = domain.location_reverse(srv.store, 30.0, 120.0, fetcher=fake)
            self.assertEqual(res1["text"], "FakeCity")
            self.assertFalse(res1["cached"])
            self.assertEqual(len(calls), 1)
            # 第二次应命中缓存，不再调用 fetcher
            res2 = domain.location_reverse(srv.store, 30.001, 120.001, fetcher=fake)
            # 30.001 与 30.0 粗化后都在 30.00 网格，故命中缓存
            self.assertTrue(res2["cached"])
            self.assertEqual(len(calls), 1, "命中缓存不应再出网")
            # fetcher 抛错 -> 502 且不写缓存
            def bad(lat, lon):
                raise _geocode.GeocodeUnavailable("network down")

            with self.assertRaises(domain.DomainError) as ctx:
                domain.location_reverse(srv.store, 31.0, 121.0, fetcher=bad)
            self.assertEqual(ctx.exception.status, 502)
            self.assertEqual(ctx.exception.code, "geocode_failed")
            self.assertIsNone(srv.store.get_geocode("nominatim", "31.00", "121.00"))
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            os.environ.pop("NETWORTH_GEOCODE_PROVIDER", None)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 6. 未观测期
    def test_unrecorded(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-unrec-"))
        srv, token, meta, _ = _make_synthetic_server(tmpdir)
        try:
            pick = meta["dates"][0]
            status, detail = srv.get(f"/api/snapshots/{pick}", token=token)
            self.assertEqual(status, 200)
            self.assertNotIn("location", detail["snapshot"])
            self.assertIn("location_absent_reason", detail["snapshot"])
            status, lst = srv.get("/api/snapshots", token=token)
            row = next(r for r in lst["snapshots"] if r["date"] == pick)
            self.assertNotIn("location", row)
            self.assertIn("location_absent_reason", row)
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 7. metrics/locations
    def test_metrics_locations(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-metrics-"))
        # 用干净库，避免合成数据的干扰
        db = Path(tmpdir) / "m.db"
        os.environ["NETWORTH_DB"] = str(db)
        os.environ["NETWORTH_SECRET"] = "secret-metrics"
        os.environ["NETWORTH_M0_OUT"] = str(tmpdir)
        store = Store(db)
        srv = ServerHandle(db)
        srv.request("POST", "/api/auth/register", {"username": "m", "password": "pass123"})
        _, sess = srv.request("POST", "/api/auth/login", {"username": "m", "password": "pass123"})
        token = sess["token"]
        try:
            # 创建账户
            status, acc = srv.request("POST", "/api/accounts", {"name": "Acc1", "kind": "asset"}, token=token)
            aid = acc["id"]
            # 创建期：A: 2021-01-01 SynthA, 2021-02-01 SynthA, 2021-03-01 无, 2021-04-01 SynthB, 2021-05-01 SynthA
            # 需要净值用于 change 计算
            def put(date, amount, loc=None):
                body = {"date": date, "items": [{"account_id": aid, "amount_cny": amount}]}
                if loc:
                    body["location"] = {"text": loc, "source": "manual"}
                return srv.request("PUT", f"/api/snapshots/{date}", body, token=token)

            put("2021-01-01", 100, "SynthA")
            put("2021-02-01", 200, "SynthA")
            put("2021-03-01", 300)  # 无地点
            put("2021-04-01", 400, "SynthB")
            put("2021-05-01", 500, "SynthA")
            status, metrics = srv.get("/api/metrics/locations", token=token)
            self.assertEqual(status, 200)
            self.assertIn("groups", metrics)
            self.assertIn("unrecorded_periods", metrics)
            self.assertEqual(metrics["unrecorded_periods"], 1)
            # SynthA 组
            gA = next(g for g in metrics["groups"] if g["text"] == "SynthA")
            self.assertEqual(gA["periods"], 3)
            self.assertEqual(gA["first_date"], "2021-01-01")
            self.assertEqual(gA["last_date"], "2021-05-01")
            # networth_change = 500-100=400
            self.assertEqual(gA["networth_change"], 400)
            # stays: SynthA 有两段：2021-01-01~2021-02-01 (2期) 和 2021-05-01~2021-05-01 (1期)
            self.assertEqual(len(gA["stays"]), 2)
            self.assertEqual(gA["stays"][0], {"from": "2021-01-01", "to": "2021-02-01", "periods": 2})
            self.assertEqual(gA["stays"][1], {"from": "2021-05-01", "to": "2021-05-01", "periods": 1})
            gB = next(g for g in metrics["groups"] if g["text"] == "SynthB")
            self.assertEqual(gB["periods"], 1)
            self.assertEqual(len(gB["stays"]), 1)
        finally:
            srv.close()
            store.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)

    # 8. backfill
    def test_backfill(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-backfill-"))
        db = Path(tmpdir) / "bf.db"
        os.environ["NETWORTH_DB"] = str(db)
        os.environ["NETWORTH_SECRET"] = "bf-secret"
        os.environ["NETWORTH_M0_OUT"] = str(tmpdir)
        store = Store(db)
        # 创建账户
        acc_id = store.create_account("BF_Acc1", "asset", category="测试")
        acc2 = store.create_account("BF_Acc2", "asset", category="测试")
        # 创建期：2021-01-01 同值地点，2021-02-01 冲突，2021-03-01 无地点
        # 备注含 地点：SynthCityA
        store.upsert_snapshot("2021-01-01", [
            {"account_id": acc_id, "amount_cny": 100, "note": "备注\n地点：SynthCityA\n说明"},
            {"account_id": acc2, "amount_cny": 200, "note": "备注\n地点：SynthCityA\n说明"},
        ], source="manual", created_at="2021-01-01T00:00:00Z", updated_at="2021-01-01T00:00:00Z")
        store.upsert_snapshot("2021-02-01", [
            {"account_id": acc_id, "amount_cny": 100, "note": "备注\n地点：SynthCityA\n说明"},
            {"account_id": acc2, "amount_cny": 200, "note": "备注\n地点：SynthCityB\n说明"},
        ], source="manual", created_at="2021-02-01T00:00:00Z", updated_at="2021-02-01T00:00:00Z")
        store.upsert_snapshot("2021-03-01", [
            {"account_id": acc_id, "amount_cny": 100, "note": "普通备注"},
        ], source="manual", created_at="2021-03-01T00:00:00Z", updated_at="2021-03-01T00:00:00Z")
        # 记录 note 原文
        before_notes = {}
        for snap in store.list_snapshots():
            for item in store.items_of(int(snap["id"])):
                before_notes[(str(snap["snapshot_date"]), int(item["account_id"]))] = item["note"]
        store.close()
        from app import location_backfill
        # dry-run 不写库
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = location_backfill.main(["--db", str(db)])
        self.assertEqual(rc, 0)
        txt = out.getvalue()
        self.assertIn("扫描期数", txt)
        self.assertIn("命中期数", txt)
        self.assertIn("待写入数", txt)
        # 验证未写库
        store = Store(db)
        self.assertIsNone(store.get_snapshot("2021-01-01")["location_text"])
        store.close()
        # --apply 应写入 1 期（2021-01-01），冲突期不写
        out2 = io.StringIO()
        with contextlib.redirect_stdout(out2):
            rc = location_backfill.main(["--db", str(db), "--apply"])
        self.assertEqual(rc, 0)
        store = Store(db)
        self.assertEqual(store.get_snapshot("2021-01-01")["location_text"], "SynthCityA")
        self.assertEqual(store.get_snapshot("2021-01-01")["location_source"], "parsed_note")
        self.assertIsNone(store.get_snapshot("2021-02-01")["location_text"], "冲突期不写")
        self.assertIsNone(store.get_snapshot("2021-03-01")["location_text"])
        # note 原文未变
        for snap in store.list_snapshots():
            for item in store.items_of(int(snap["id"])):
                key = (str(snap["snapshot_date"]), int(item["account_id"]))
                self.assertEqual(item["note"], before_notes[key], "不得改动 note 原文")
        store.close()
        # 二次 --apply 0 变更
        out3 = io.StringIO()
        with contextlib.redirect_stdout(out3):
            rc = location_backfill.main(["--db", str(db), "--apply"])
        self.assertEqual(rc, 0)
        self.assertIn("写入数：0", out3.getvalue())
        shutil.rmtree(tmpdir, ignore_errors=True)
        for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
            os.environ.pop(k, None)

    # 9. 锚点
    def test_anchors_not_broken(self):
        """锚点回归不写死真实净值：断言「服务端重算 == 本期明细合计」与期数守恒。

        为什么不用硬编码金额：仓库历史曾因真实财务数据外泄被重写，测试里写死真实净值等于
        把它重新提交进 git。重算一致性 + 明细条数守恒同样能抓住「净值口径被改坏」的回归。
        """
        tmpdir = Path(tempfile.mkdtemp(prefix="loc-anchor-"))
        srv, token, meta, _ = _make_synthetic_server(tmpdir)
        try:
            picks = (meta["dates"][0], meta["dates"][len(meta["dates"]) // 2], meta["dates"][-1])
            for date_text in picks:
                status, payload = srv.get(f"/api/snapshots/{date_text}", token=token)
                self.assertEqual(status, 200, date_text)
                snap, items = payload["snapshot"], payload["items"]
                total = round(sum(float(i["amount_cny"]) for i in items), 2)
                self.assertAlmostEqual(snap["networth"], total, places=2,
                                       msg=f"{date_text} 净值必须等于本期明细合计")
                self.assertEqual(snap["item_count"], len(items), date_text)
                # 额外校验：明细合计应与生成数据集的 computed 一致（相对断言，非硬编码）
                self.assertAlmostEqual(snap["networth"], meta["computed"][date_text], places=2)
            self.assertGreaterEqual(len(srv.get("/api/snapshots", token=token)[1]["snapshots"]), meta["period_count"])
        finally:
            srv.close()
            shutil.rmtree(tmpdir, ignore_errors=True)
            for k in ("NETWORTH_DB", "NETWORTH_SECRET", "NETWORTH_M0_OUT"):
                os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
