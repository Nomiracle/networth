"""networth 数据访问层：SQLite 建表（幂等迁移）与全部 CRUD。

表结构严格按 docs/m1-spec.md 第 2 节实现（额外增加一张 reconcile_period 表，
用于把 M0 对账结果（总览值 vs 重算值）落进库，供 /api/metrics/reconcile 读取；
同时导入器仍会按契约写出 data/out/reconcile-imported.json）。

线程安全：ThreadingHTTPServer 下每个请求一个线程，这里用一把可重入锁把
所有 SQL 串行化（SQLite 连接本身不是线程安全的），写操作统一走 tx() 事务。
"""
from __future__ import annotations

import json as _json
import sqlite3
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 建表 SQL

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  token_digest TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  category TEXT,
  subclass TEXT,
  kind TEXT NOT NULL CHECK(kind IN ('asset','liability')),
  currency TEXT DEFAULT 'CNY',
  is_liquid INTEGER DEFAULT 0,
  is_counted INTEGER DEFAULT 1,
  sort INTEGER DEFAULT 0,
  active_from TEXT,
  active_to TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS account_alias (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL,
  alias TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot (
  id INTEGER PRIMARY KEY,
  snapshot_date TEXT UNIQUE NOT NULL,
  status TEXT DEFAULT 'final',
  default_fx_rate REAL,
  market_note TEXT,
  data_quality_flags TEXT,
  source TEXT DEFAULT 'manual',
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS snapshot_item (
  id INTEGER PRIMARY KEY,
  snapshot_id INTEGER NOT NULL,
  account_id INTEGER NOT NULL,
  amount_cny REAL NOT NULL,
  amount_usd REAL,
  fx_rate REAL,
  note TEXT,
  auto_filled INTEGER DEFAULT 0,
  flags TEXT
);
CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY,
  event_date TEXT NOT NULL,
  kind TEXT NOT NULL,
  amount_cny REAL NOT NULL,
  note TEXT,
  created_at TEXT
);
-- M0 对账结果（总览序列 vs 服务端重算），每期一行
CREATE TABLE IF NOT EXISTS reconcile_period (
  id INTEGER PRIMARY KEY,
  period_date TEXT UNIQUE NOT NULL,
  imported REAL,
  computed REAL,
  diff REAL,
  flags TEXT,
  item_count INTEGER,
  extra_table INTEGER DEFAULT 0,
  right_numeric INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS snapshot_item_uq ON snapshot_item(snapshot_id, account_id);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS fx_rate (
  req_date TEXT PRIMARY KEY,
  rate REAL NOT NULL,
  source TEXT NOT NULL,
  source_date TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshot_item_account_idx ON snapshot_item(account_id);
CREATE INDEX IF NOT EXISTS snapshot_date_idx ON snapshot(snapshot_date);
CREATE INDEX IF NOT EXISTS token_user_idx ON token(user_id);
CREATE INDEX IF NOT EXISTS account_alias_account_idx ON account_alias(account_id);
CREATE INDEX IF NOT EXISTS event_date_idx ON event(event_date);
"""

# 账户表可更新字段白名单（PATCH /api/accounts/{id}）
ACCOUNT_UPDATABLE = (
    "name", "category", "subclass", "kind", "currency", "is_liquid",
    "is_counted", "sort", "active_from", "active_to", "note",
)


class ConflictError(Exception):
    """唯一约束冲突（用户名/账户名/别名 重复）。"""


class Store:
    """SQLite 封装。每条 SQL 都在锁内执行；批量写用 tx()。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    # ------------------------------------------------------------ 基础工具

    def init_schema(self) -> None:
        """幂等建表 + 迁移。"""
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """事务：异常时整体回滚。"""
        with self._lock:
            try:
                yield self.conn
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, params).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def _run(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def checkpoint(self) -> None:
        """把 WAL 合并回主库文件。

        WAL 模式下已提交数据可能仍在 `*.db-wal` 里，直接 `cp app.db backup.db`
        会复制到较旧的状态（实测：62 期库被复制成 5 期，且两个文件字节数完全相同）。
        关闭前做一次 TRUNCATE 检查点，停止后的 .db 就是完整单文件。
        """
        try:
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.commit()
        except sqlite3.Error as exc:  # 检查点失败不应阻塞退出
            print(f"[networth] wal_checkpoint 失败（忽略）：{exc}", file=sys.stderr)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------ 计数

    def counts(self) -> dict[str, int]:
        """各表行数，供 /api/healthz 与导入校验使用。"""
        tables = ("user", "token", "account", "account_alias",
                  "snapshot", "snapshot_item", "event", "reconcile_period")
        out: dict[str, int] = {}
        for table in tables:
            row = self._one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        out["periods"] = out["snapshot"]
        out["accounts"] = out["account"]
        return out

    # ------------------------------------------------------------ user / token

    def user_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM user")
        return int(row["n"]) if row else 0

    def get_user_by_name(self, username: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM user WHERE username = ?", (username,))

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM user WHERE id = ?", (user_id,))

    def create_user(self, username: str, password_hash: str, created_at: str) -> int:
        try:
            cur = self._run(
                "INSERT INTO user(username, password_hash, created_at) VALUES (?,?,?)",
                (username, password_hash, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"用户名已存在：{username}") from exc
        return int(cur.lastrowid or 0)

    def create_token(self, user_id: int, digest: str, created_at: str, expires_at: str) -> int:
        cur = self._run(
            "INSERT INTO token(user_id, token_digest, created_at, expires_at) VALUES (?,?,?,?)",
            (user_id, digest, created_at, expires_at),
        )
        return int(cur.lastrowid or 0)

    def find_token(self, digest: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM token WHERE token_digest = ?", (digest,))

    def revoke_token(self, digest: str, revoked_at: str) -> bool:
        cur = self._run(
            "UPDATE token SET revoked_at = ? WHERE token_digest = ? AND revoked_at IS NULL",
            (revoked_at, digest),
        )
        return cur.rowcount > 0

    def purge_expired_tokens(self, now_iso: str) -> int:
        cur = self._run("DELETE FROM token WHERE expires_at < ?", (now_iso,))
        return cur.rowcount

    # ------------------------------------------------------------ account / alias

    def list_accounts(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM account ORDER BY sort, name")

    def get_account(self, account_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM account WHERE id = ?", (account_id,))

    def get_account_by_name(self, name: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM account WHERE name = ?", (name,))

    def aliases_by_account(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for row in self._all("SELECT account_id, alias FROM account_alias ORDER BY id"):
            out.setdefault(int(row["account_id"]), []).append(row["alias"])
        return out

    def create_account(self, name: str, kind: str, **fields: Any) -> int:
        """新增账户；name 重复时抛 ConflictError。"""
        cols = ["name", "kind"] + [c for c in ACCOUNT_UPDATABLE if c in fields and c not in ("name", "kind")]
        params = [name, kind] + [fields[c] for c in cols[2:]]
        sql = f"INSERT INTO account({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        try:
            cur = self._run(sql, params)
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"账户已存在：{name}") from exc
        return int(cur.lastrowid or 0)

    def update_account(self, account_id: int, fields: dict[str, Any]) -> bool:
        sets, params = [], []
        for key in ACCOUNT_UPDATABLE:
            if key in fields and fields[key] is not None:
                sets.append(f"{key} = ?")
                params.append(fields[key])
        if not sets:
            return False
        params.append(account_id)
        try:
            cur = self._run(f"UPDATE account SET {', '.join(sets)} WHERE id = ?", params)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("账户名与已有账户重复") from exc
        return cur.rowcount > 0

    def upsert_account(self, name: str, kind: str, fields: dict[str, Any]) -> int:
        """按 name 幂等 upsert，返回 account_id。"""
        row = self.get_account_by_name(name)
        if row:
            self.update_account(int(row["id"]), {**fields, "kind": kind})
            return int(row["id"])
        return self.create_account(name, kind, **fields)

    def add_alias(self, account_id: int, alias: str) -> bool:
        """新增别名；别名已被占用时抛 ConflictError。"""
        try:
            self._run("INSERT INTO account_alias(account_id, alias) VALUES (?,?)", (account_id, alias))
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"别名已存在：{alias}") from exc
        return True

    # ------------------------------------------------------------ snapshot / item

    def list_snapshots(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM snapshot ORDER BY snapshot_date")

    def get_snapshot(self, date: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM snapshot WHERE snapshot_date = ?", (date,))

    def snapshot_totals(self) -> list[dict[str, Any]]:
        """每期重算：总资产 / 总负债（有符号，通常为负）/ 净资产 / 明细数。

        净资产 = Σ 该期所有明细行 amount_cny（负债块正号行按原样加），
        与 total_assets + total_liabilities 恒等。
        """
        rows = self._all(
            """
            SELECT s.id AS sid, s.snapshot_date AS date, s.status AS status,
                   s.default_fx_rate AS fx, s.market_note AS market_note,
                   s.data_quality_flags AS flags, s.source AS source,
                   s.created_at AS created_at, s.updated_at AS updated_at,
                   COALESCE(SUM(CASE WHEN a.kind='asset' THEN i.amount_cny ELSE 0 END), 0) AS total_assets,
                   COALESCE(SUM(CASE WHEN a.kind='liability' THEN i.amount_cny ELSE 0 END), 0) AS total_liabilities,
                   COALESCE(SUM(i.amount_cny), 0) AS networth,
                   COUNT(i.id) AS item_count
            FROM snapshot s
            LEFT JOIN snapshot_item i ON i.snapshot_id = s.id
            LEFT JOIN account a ON a.id = i.account_id
            GROUP BY s.id
            ORDER BY s.snapshot_date
            """
        )
        return [dict(r) for r in rows]

    def get_snapshot_totals(self, date: str) -> dict[str, Any] | None:
        for row in self.snapshot_totals():
            if row["date"] == date:
                return row
        return None

    def items_of(self, snapshot_id: int) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT i.*, a.name AS account_name, a.kind AS kind, a.category AS category,
                   a.subclass AS subclass, a.sort AS sort, a.is_counted AS is_counted
            FROM snapshot_item i JOIN account a ON a.id = i.account_id
            WHERE i.snapshot_id = ?
            ORDER BY a.sort, a.name
            """,
            (snapshot_id,),
        )

    def account_series(self, account_id: int) -> list[dict[str, Any]]:
        rows = self._all(
            """
            SELECT s.snapshot_date AS date, i.amount_cny AS amount, i.amount_usd AS amount_usd,
                   i.fx_rate AS fx_rate, i.note AS note, i.flags AS flags
            FROM snapshot_item i JOIN snapshot s ON s.id = i.snapshot_id
            WHERE i.account_id = ?
            ORDER BY s.snapshot_date
            """,
            (account_id,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ meta（键值）
    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._run(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------ 汇率缓存
    def snapshot_fx_rows(self) -> list[tuple[str, float | None]]:
        """各期次日期与已填写的期级汇率（回填脚本用）。"""
        return [(str(r["snapshot_date"]), r["default_fx_rate"])
                for r in self._all("SELECT snapshot_date, default_fx_rate FROM snapshot "
                                   "ORDER BY snapshot_date")]

    def set_default_fx_rate(self, date_text: str, rate: float) -> int:
        """只回填空缺的期级汇率（已有值不动），返回受影响行数。"""
        cur = self._run("UPDATE snapshot SET default_fx_rate = ? "
                        "WHERE snapshot_date = ? AND default_fx_rate IS NULL",
                        (float(rate), date_text))
        return cur.rowcount or 0

    def get_fx_rate(self, req_date: str) -> sqlite3.Row | None:
        """某日（快照日期）已缓存的汇率。"""
        return self._one("SELECT * FROM fx_rate WHERE req_date = ?", (req_date,))

    def latest_fx_rate(self, req_date: str) -> sqlite3.Row | None:
        """不晚于该日期的最近一条缓存（抓取失败时的兜底，调用方需标注非当日值）。"""
        return self._one("SELECT * FROM fx_rate WHERE req_date <= ? ORDER BY req_date DESC LIMIT 1",
                         (req_date,))

    def upsert_fx_rate(self, req_date: str, rate: float, source: str,
                       source_date: str, fetched_at: str) -> None:
        self._run(
            "INSERT INTO fx_rate(req_date, rate, source, source_date, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(req_date) DO UPDATE SET rate = excluded.rate, source = excluded.source, "
            "source_date = excluded.source_date, fetched_at = excluded.fetched_at",
            (req_date, float(rate), source, source_date, fetched_at),
        )

    # ------------------------------------------------------------ 投资日志备注
    def note_rows(self) -> list[dict[str, Any]]:
        """所有非空明细备注，带期次与账户信息（投资日志聚合用）。"""
        rows = self._all(
            """
            SELECT s.snapshot_date AS date, i.account_id AS account_id,
                   a.name AS account, a.kind AS kind, a.category AS category,
                   a.subclass AS subclass, i.amount_cny AS amount_cny,
                   i.amount_usd AS amount_usd, i.fx_rate AS fx_rate,
                   TRIM(i.note) AS note
            FROM snapshot_item i
            JOIN snapshot s ON s.id = i.snapshot_id
            JOIN account a ON a.id = i.account_id
            WHERE i.note IS NOT NULL AND TRIM(i.note) <> ''
            ORDER BY s.snapshot_date DESC, a.sort, a.name
            """
        )
        return [dict(r) for r in rows]

    def account_summaries(self) -> dict[int, dict[str, Any]]:
        """每个账户的：期数 / 最新日期 / 最新金额 / 变动尾长(连续未变化期数)。"""
        rows = self._all(
            """
            SELECT i.account_id AS account_id, s.snapshot_date AS date, i.amount_cny AS amount
            FROM snapshot_item i JOIN snapshot s ON s.id = i.snapshot_id
            ORDER BY i.account_id, s.snapshot_date
            """
        )
        grouped: dict[int, list[tuple[str, float]]] = {}
        for row in rows:
            grouped.setdefault(int(row["account_id"]), []).append((row["date"], float(row["amount"])))
        out: dict[int, dict[str, Any]] = {}
        for account_id, points in grouped.items():
            unchanged = 1
            for (_, prev), (_, cur) in zip(reversed(points[:-1]), reversed(points)):
                if abs(prev - cur) < 1e-9:
                    unchanged += 1
                else:
                    break
            out[account_id] = {
                "periods": len(points),
                "last_date": points[-1][0],
                "last_amount": points[-1][1],
                "unchanged_tail": unchanged,
                "first_date": points[0][0],
            }
        return out

    def upsert_snapshot(
        self,
        date: str,
        items: Iterable[dict[str, Any]],
        *,
        status: str = "final",
        default_fx_rate: float | None = None,
        market_note: str | None = None,
        flags: list[str] | None = None,
        source: str = "manual",
        created_at: str | None = None,
        updated_at: str | None = None,
        keep_created_at: bool = True,
    ) -> tuple[int, bool]:
        """按 date 幂等 upsert 一期快照；明细行整期 delete + insert。返回 (id, created)。"""
        flags_text = _json.dumps(flags or [], ensure_ascii=False) if flags is not None else None
        items = list(items)
        with self.tx() as conn:
            row = conn.execute("SELECT * FROM snapshot WHERE snapshot_date = ?", (date,)).fetchone()
            created = row is None
            if created:
                cur = conn.execute(
                    """INSERT INTO snapshot(snapshot_date, status, default_fx_rate, market_note,
                                            data_quality_flags, source, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (date, status, default_fx_rate, market_note, flags_text, source,
                     created_at or updated_at, updated_at),
                )
                snapshot_id = int(cur.lastrowid or 0)
            else:
                snapshot_id = int(row["id"])
                conn.execute(
                    """UPDATE snapshot SET status=?, default_fx_rate=?, market_note=?, data_quality_flags=?,
                                          source=?, created_at=?, updated_at=? WHERE id=?""",
                    (status, default_fx_rate, market_note,
                     flags_text if flags is not None else row["data_quality_flags"],
                     source or row["source"],
                     (row["created_at"] if keep_created_at else created_at) or created_at,
                     updated_at, snapshot_id),
                )
                conn.execute("DELETE FROM snapshot_item WHERE snapshot_id = ?", (snapshot_id,))
            for item in items:
                conn.execute(
                    """INSERT INTO snapshot_item(snapshot_id, account_id, amount_cny, amount_usd,
                                                 fx_rate, note, auto_filled, flags)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        snapshot_id,
                        int(item["account_id"]),
                        float(item["amount_cny"]),
                        item.get("amount_usd"),
                        item.get("fx_rate"),
                        item.get("note"),
                        int(item.get("auto_filled") or 0),
                        item.get("flags"),
                    ),
                )
        return snapshot_id, created

    def delete_snapshot(self, date: str) -> bool:
        with self.tx() as conn:
            row = conn.execute("SELECT id FROM snapshot WHERE snapshot_date = ?", (date,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM snapshot_item WHERE snapshot_id = ?", (int(row["id"]),))
            conn.execute("DELETE FROM snapshot WHERE id = ?", (int(row["id"]),))
        return True

    # ------------------------------------------------------------ event

    def list_events(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM event ORDER BY event_date, id")

    def add_event(self, event_date: str, kind: str, amount_cny: float,
                  note: str | None, created_at: str) -> bool:
        """幂等新增事件（同日期+类型+金额+备注 视为同一条）。"""
        exists = self._one(
            """SELECT id FROM event WHERE event_date=? AND kind=? AND ROUND(amount_cny,2)=ROUND(?,2)
               AND COALESCE(note,'')=COALESCE(?,'')""",
            (event_date, kind, amount_cny, note),
        )
        if exists:
            return False
        self._run(
            "INSERT INTO event(event_date, kind, amount_cny, note, created_at) VALUES (?,?,?,?,?)",
            (event_date, kind, float(amount_cny), note, created_at),
        )
        return True

    # ------------------------------------------------------------ reconcile

    def replace_reconcile(self, rows: Iterable[dict[str, Any]]) -> int:
        """整体替换 M0 对账结果。"""
        rows = list(rows)
        with self.tx() as conn:
            conn.execute("DELETE FROM reconcile_period")
            for row in rows:
                conn.execute(
                    """INSERT INTO reconcile_period(period_date, imported, computed, diff, flags,
                                                    item_count, extra_table, right_numeric)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        row["date"],
                        row.get("imported"),
                        row.get("computed"),
                        row.get("diff"),
                        _json_join(row.get("flags")),
                        row.get("items"),
                        1 if row.get("extra_table") else 0,
                        row.get("right_numeric") or 0,
                    ),
                )
        return len(rows)

    def list_reconcile(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM reconcile_period ORDER BY period_date")


def _json_join(flags: Any) -> str | None:
    """flags 列表 -> JSON 文本（保持可解析）。"""
    if flags is None:
        return None
    return _json.dumps(list(flags), ensure_ascii=False)
