"""networth 领域逻辑：鉴权、口径重算、快照/指标/导入导出。

计算口径（docs/m1-spec.md 第 1 节，已用 62 期历史验证为零差异）：

    净资产 = Σ 该期所有明细行的有符号 amount_cny   ← 负债块里的正号行按原样加
    total_assets       = Σ kind=asset
    total_liabilities  = Σ kind=liability（通常为负数）
    networth           = total_assets + total_liabilities

小计/资产总计/净资产行一律不导入、不接受前端传入，服务端永远重算。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Any

from . import fx
from .config import Settings
from .store import ConflictError, Store

# ---------------------------------------------------------------- 通用工具

MONEY_EPS = 0.005  # 金额比较容差（分）


class DomainError(Exception):
    """业务错误：带 HTTP 状态码与错误码。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def now_iso() -> str:
    """当前 UTC 时间（秒精度 ISO8601，带 Z）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def r2(value: Any) -> float | None:
    """金额统一保留两位小数。"""
    if value is None:
        return None
    return round(float(value) + 0.0, 2)


def r6(value: Any) -> float | None:
    """汇率保留六位小数（原表里存在 7.17999999999999 这类浮点尾巴）。"""
    if value is None:
        return None
    return round(float(value), 6)


def normalize_date(value: Any) -> str:
    """把 2025-8-18 / 20250818 / date 统一成 YYYY-MM-DD。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date_cls):
        return value.isoformat()
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise DomainError(400, "bad_date", f"日期格式不合法：{value!r}（应为 YYYY-MM-DD）")


# ---------------------------------------------------------------- 密码与令牌

def hash_password(password: str, secret: bytes) -> str:
    """scrypt(加盐 + 服务端 pepper) 哈希；scrypt 不可用时退回 pbkdf2_sha256。"""
    salt = secrets.token_bytes(16)
    material = password.encode("utf-8") + b"\x00" + secret
    try:
        digest = hashlib.scrypt(material, salt=salt, n=2 ** 14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
        return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"
    except (ValueError, MemoryError, OSError):
        digest = hashlib.pbkdf2_hmac("sha256", material, salt, 200_000, dklen=32)
        return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str, secret: bytes) -> bool:
    """校验密码（失败一律返回 False，不区分用户不存在/密码错）。"""
    try:
        scheme, *rest = (stored or "").split("$")
        material = password.encode("utf-8") + b"\x00" + secret
        if scheme == "scrypt":
            n, rr, p, salt_hex, hash_hex = rest
            digest = hashlib.scrypt(material, salt=bytes.fromhex(salt_hex), n=int(n), r=int(rr),
                                    p=int(p), dklen=32, maxmem=64 * 1024 * 1024)
        elif scheme == "pbkdf2_sha256":
            rounds, salt_hex, hash_hex = rest
            digest = hashlib.pbkdf2_hmac("sha256", material, bytes.fromhex(salt_hex),
                                         int(rounds), dklen=32)
        else:
            return False
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError, AttributeError):
        return False


def token_digest(token: str) -> str:
    """令牌以 sha256 摘要入库，明文只在登录时返回一次。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# 密钥指纹：口令哈希把 NETWORTH_SECRET 当 pepper 用（password + \0 + secret），
# 所以一旦密钥被换掉，已有账号将永远无法登录（且不可恢复）。库里只存指纹，
# 启动时比对，能立刻发现「.env 被重建/丢失」这类事故。
SECRET_META_KEY = "secret_fingerprint"


def secret_fingerprint(settings: Settings) -> str:
    return hashlib.sha256(settings.secret).hexdigest()[:16]


def secret_mismatch(store: Store, settings: Settings) -> bool:
    """库里记录的指纹与当前密钥不一致 → 已有账号的密码校验会全部失败。"""
    stored = store.get_meta(SECRET_META_KEY)
    return bool(stored) and stored != secret_fingerprint(settings)


def remember_secret(store: Store, settings: Settings) -> None:
    """记录密钥指纹（只补空缺，不覆盖不同值；首次注册或首次登录成功时调用）。"""
    if store.get_meta(SECRET_META_KEY) is None:
        store.set_meta(SECRET_META_KEY, secret_fingerprint(settings))


def create_token(store: Store, settings: Settings, user_id: int) -> dict[str, Any]:
    """签发令牌，返回 {token, expires_at}。"""
    plain = secrets.token_urlsafe(32)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=settings.token_ttl_days)
    created_iso = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_iso = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
    store.purge_expired_tokens(created_iso)
    store.create_token(user_id, token_digest(plain), created_iso, expires_iso)
    return {"token": plain, "expires_at": expires_iso}


def authenticate(store: Store, settings: Settings, authorization: str | None) -> sqlite3.Row:
    """校验 Authorization: Bearer <token>，返回 user 行；失败抛 401。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise DomainError(401, "unauthorized", "缺少 Bearer 令牌")
    plain = authorization.split(" ", 1)[1].strip()
    if not plain:
        raise DomainError(401, "unauthorized", "令牌为空")
    row = store.find_token(token_digest(plain))
    if row is None:
        raise DomainError(401, "unauthorized", "令牌无效")
    if row["revoked_at"]:
        raise DomainError(401, "unauthorized", "令牌已注销")
    if str(row["expires_at"]) < now_iso():
        raise DomainError(401, "unauthorized", "令牌已过期")
    user = store.get_user(int(row["user_id"]))
    if user is None:
        raise DomainError(401, "unauthorized", "用户不存在")
    return user


def registration_open(store: Store, settings: Settings) -> bool:
    """注册是否开放：库中无用户时总是开放；否则要求 ALLOW_REGISTRATION=1。"""
    return store.user_count() == 0 or bool(settings.allow_registration)


def register(store: Store, settings: Settings, username: str, password: str) -> dict[str, Any]:
    """注册：库中无用户时总是允许；否则要求 ALLOW_REGISTRATION=1。"""
    username = (username or "").strip()
    password = password or ""
    if not username:
        raise DomainError(400, "bad_request", "用户名不能为空")
    if len(username) > 64:
        raise DomainError(400, "bad_request", "用户名过长（最多 64 字符）")
    if len(password) < 6:
        raise DomainError(400, "bad_request", "密码长度至少 6 位")
    if store.user_count() > 0 and not settings.allow_registration:
        raise DomainError(403, "registration_closed",
                          "注册已关闭（首个账号已存在；如需开放请设置 ALLOW_REGISTRATION=1）")
    try:
        user_id = store.create_user(username, hash_password(password, settings.secret), now_iso())
    except ConflictError as exc:
        raise DomainError(409, "conflict", str(exc)) from exc
    remember_secret(store, settings)
    return {"id": user_id, "username": username}


def login(store: Store, settings: Settings, username: str, password: str) -> dict[str, Any]:
    """登录：返回明文令牌 + 过期时间。"""
    user = store.get_user_by_name((username or "").strip())
    if user is None or not verify_password(password or "", user["password_hash"], settings.secret):
        raise DomainError(401, "unauthorized", "用户名或密码错误")
    # 老库没有指纹时，用一次成功登录把当前密钥记下来（不覆盖已有指纹）
    remember_secret(store, settings)
    token = create_token(store, settings, int(user["id"]))
    return {"token": token["token"], "expires_at": token["expires_at"], "username": user["username"]}


def logout(store: Store, authorization: str) -> dict[str, Any]:
    """注销当前令牌。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise DomainError(401, "unauthorized", "缺少 Bearer 令牌")
    plain = authorization.split(" ", 1)[1].strip()
    store.revoke_token(token_digest(plain), now_iso())
    return {"ok": True}


# ---------------------------------------------------------------- 口径重算

def period_totals(kind_amounts: list[tuple[str, float]]) -> dict[str, float]:
    """(kind, amount) 列表 -> 三个口径值（服务端重算唯一入口）。"""
    assets = sum(amount for kind, amount in kind_amounts if kind == "asset")
    liabilities = sum(amount for kind, amount in kind_amounts if kind == "liability")
    return {
        "total_assets": r2(assets) or 0.0,
        "total_liabilities": r2(liabilities) or 0.0,
        "networth": r2(assets + liabilities) or 0.0,
    }


def _flags_of(raw: Any) -> list[str]:
    """data_quality_flags 文本 -> 列表。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
        return [str(parsed)]
    except (ValueError, TypeError):
        return [str(raw)] if str(raw) else []


def account_views(store: Store) -> list[dict[str, Any]]:
    """GET /api/accounts 的数据。"""
    aliases = store.aliases_by_account()
    summaries = store.account_summaries()
    out = []
    for row in store.list_accounts():
        aid = int(row["id"])
        info = summaries.get(aid, {})
        out.append({
            "id": aid,
            "name": row["name"],
            "category": row["category"],
            "subclass": row["subclass"],
            "kind": row["kind"],
            "currency": row["currency"],
            "is_liquid": int(row["is_liquid"] or 0),
            "is_counted": int(row["is_counted"] or 0),
            "sort": int(row["sort"] or 0),
            "active_from": row["active_from"],
            "active_to": row["active_to"],
            "note": row["note"],
            "aliases": aliases.get(aid, []),
            "last_amount": r2(info.get("last_amount")),
            "last_date": info.get("last_date"),
            "periods": int(info.get("periods") or 0),
            "unchanged_tail": int(info.get("unchanged_tail") or 0),
        })
    return out


def account_series(store: Store, account_id: int) -> dict[str, Any]:
    """GET /api/accounts/{id}/series。"""
    row = store.get_account(account_id)
    if row is None:
        raise DomainError(404, "not_found", f"账户不存在：{account_id}")
    # 与 /api/accounts 保持同一套账户字段：否则前端详情页的「期数 / 计入统计 /
    # 未变动期数」会读成空值（曾实测显示「期数 0、计入统计 否」）
    info = store.account_summaries().get(account_id, {})
    points = []
    prev: float | None = None
    for item in store.account_series(account_id):
        amount = r2(item["amount"])
        points.append({
            "date": item["date"],
            "amount": amount,
            "delta": None if prev is None else r2((amount or 0.0) - prev),
            "amount_usd": item["amount_usd"],
            "fx_rate": r6(item["fx_rate"]),
            "note": item["note"],
        })
        prev = amount or 0.0
    return {
        "account": {
            "id": int(row["id"]), "name": row["name"], "kind": row["kind"],
            "category": row["category"], "subclass": row["subclass"],
            "currency": row["currency"],
            "is_counted": int(row["is_counted"] or 0),
            "active_from": row["active_from"], "active_to": row["active_to"],
            "aliases": store.aliases_by_account().get(account_id, []),
            "last_amount": r2(info.get("last_amount")),
            "last_date": info.get("last_date"),
            "periods": int(info.get("periods") or 0),
            "unchanged_tail": int(info.get("unchanged_tail") or 0),
        },
        "points": points,
    }


# 同一期里同一段备注被 >= 2 个账户引用时，视为「期级投资日志」（原表的日志就写在明细备注上、
# 并在一期里重复出现在多行）；只出现一次的视为该账户自己的备注。
JOURNAL_SHARED_MIN = 2


def strip_note_label(text: str) -> str:
    """去掉原表单元格里自带的「备注」标签行（只影响展示，库里保留原文）。"""
    lines = (text or "").splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines) and lines[idx].strip().rstrip("：:") == "备注":
        return "\n".join(lines[idx + 1:]).strip()
    return (text or "").strip()


def note_title(text: str) -> str:
    """备注首行（跳过原表单元格自带的「备注」标签行），用于列表紧凑展示。"""
    for line in strip_note_label(text).splitlines():
        if line.strip():
            return line.strip()
    return ""


def journal(store: Store, *, scope: str = "all", account_id: int | None = None,
            keyword: str = "", limit: int = 300) -> dict[str, Any]:
    """GET /api/journal —— 投资日志（把明细行备注按「期 + 文本」聚合去重）。

    返回两类条目：
      - scope="journal"：同一期被多个账户引用的同一段文本 → 期级投资日志（去重后只出现一次）
      - scope="account"：只挂在一个账户上的文本 → 账户备注
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in store.note_rows():
        key = (row["date"], row["note"])
        g = groups.get(key)
        if g is None:
            g = {"date": row["date"], "text": row["note"], "title": note_title(row["note"]),
                 "accounts": [], "amount_cny": 0.0, "kinds": set(), "categories": set(),
                 "amount_usd": 0.0}
            groups[key] = g
        g["accounts"].append({"id": int(row["account_id"]), "name": row["account"],
                              "kind": row["kind"], "amount_cny": r2(row["amount_cny"])})
        g["amount_cny"] = r2((g["amount_cny"] or 0.0) + (row["amount_cny"] or 0.0))
        g["amount_usd"] = r2((g["amount_usd"] or 0.0) + (row["amount_usd"] or 0.0))
        g["kinds"].add(row["kind"])
        if row["category"]:
            g["categories"].add(row["category"])

    entries: list[dict[str, Any]] = []
    kw = (keyword or "").strip().lower()
    for g in groups.values():
        shared = len(g["accounts"])
        entry_scope = "journal" if shared >= JOURNAL_SHARED_MIN else "account"
        if scope in ("journal", "account") and entry_scope != scope:
            continue
        if account_id is not None and all(a["id"] != account_id for a in g["accounts"]):
            continue
        if kw and kw not in g["text"].lower() and not any(kw in a["name"].lower() for a in g["accounts"]):
            continue
        entries.append({
            "date": g["date"],
            "scope": entry_scope,
            "title": g["title"],
            # 展示用的正文：去掉原表单元格自带的「备注」标签行（库里保留原文）
            "text": strip_note_label(g["text"]),
            "accounts": g["accounts"],
            "account_count": shared,
            "amount_cny": r2(g["amount_cny"]),
            "amount_usd": r2(g["amount_usd"]),
            "kinds": sorted(g["kinds"]),
            "categories": sorted(g["categories"]),
            "lines": len([ln for ln in g["text"].splitlines() if ln.strip()]),
        })

    # 期次倒序；同期内按覆盖账户数倒序
    entries.sort(key=lambda e: (e["date"], e["account_count"]), reverse=True)
    total = len(entries)
    journal_count = sum(1 for e in entries if e["scope"] == "journal")
    account_count = total - journal_count
    if limit and total > limit:
        entries = entries[:limit]
    return {
        "entries": entries,
        "total": total,
        "journal_count": journal_count,
        "account_note_count": account_count,
        "period_count": len({e["date"] for e in entries}),
        "truncated": total > len(entries),
    }


def snapshot_list(store: Store) -> list[dict[str, Any]]:
    """GET /api/snapshots。"""
    # 期级备注在原表里基本都是空的，真正的说明写在明细行的「备注」上 →
    # 取该期出现次数最多的一段备注（通常就是那期的投资日志）的首行作为备注摘要
    per_date: dict[str, dict[str, int]] = {}
    for row in store.note_rows():
        per_date.setdefault(row["date"], {})
        per_date[row["date"]][row["note"]] = per_date[row["date"]].get(row["note"], 0) + 1
    summary: dict[str, str] = {}
    for date_text, counts in per_date.items():
        best = max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
        title = note_title(best)
        if title:
            summary[date_text] = title[:48]

    out = []
    for row in store.snapshot_totals():
        out.append({
            "date": row["date"],
            "status": row["status"] or "final",
            "networth": r2(row["networth"]),
            "total_assets": r2(row["total_assets"]),
            "total_liabilities": r2(row["total_liabilities"]),
            "item_count": int(row["item_count"]),
            "market_note": row["market_note"],
            "note_summary": summary.get(row["date"]),
            "data_quality_flags": _flags_of(row["flags"]),
            "source": row["source"],
        })
    return out


def snapshot_detail(store: Store, date_text: str) -> dict[str, Any]:
    """GET /api/snapshots/{date}。"""
    date_text = normalize_date(date_text)
    snap = store.get_snapshot(date_text)
    if snap is None:
        raise DomainError(404, "not_found", f"快照不存在：{date_text}")
    totals = store.get_snapshot_totals(date_text) or {}
    items = []
    for item in store.items_of(int(snap["id"])):
        flags = _flags_of(item["flags"])
        items.append({
            "account_id": int(item["account_id"]),
            "account": item["account_name"],
            "kind": item["kind"],
            "category": item["category"],
            "subclass": item["subclass"],
            "amount_cny": r2(item["amount_cny"]),
            "amount_usd": item["amount_usd"],
            "fx_rate": r6(item["fx_rate"]),
            "note": item["note"],
            "auto_filled": int(item["auto_filled"] or 0),
            "flags": flags,
        })
    snapshot = {
        "date": snap["snapshot_date"],
        "status": snap["status"] or "final",
        "default_fx_rate": r6(snap["default_fx_rate"]),
        "market_note": snap["market_note"],
        "data_quality_flags": _flags_of(snap["data_quality_flags"]),
        "source": snap["source"],
        "created_at": snap["created_at"],
        "updated_at": snap["updated_at"],
        "networth": r2(totals.get("networth")),
        "total_assets": r2(totals.get("total_assets")),
        "total_liabilities": r2(totals.get("total_liabilities")),
        "item_count": int(totals.get("item_count") or 0),
    }
    return {"snapshot": snapshot, "items": items}


def carry_forward(store: Store, from_date: str | None = None) -> dict[str, Any]:
    """POST /api/snapshots/carry-forward：把上一期明细带到新的一期（不落库）。"""
    snaps = store.snapshot_totals()
    if not snaps:
        return {"date_default": date_cls.today().isoformat(), "fx_rate_default": None,
                "source_date": None, "items": []}
    if from_date:
        date_text = normalize_date(from_date)
        source = next((s for s in snaps if s["date"] == date_text), None)
        if source is None:
            raise DomainError(404, "not_found", f"源快照不存在：{date_text}")
    else:
        source = snaps[-1]
        date_text = str(source["date"])

    today = date_cls.today()
    last_date = date_cls.fromisoformat(str(snaps[-1]["date"]))
    # 建议日期不得晚于今天。此前写成 `today if today > last_date else last_date + 1 天`：
    # 最后一期就是今天时（刚存完一期再进录入页），建议日期会被推到「明天」，
    # 用户再提交一次就凭空多出一期。已有该日期的期次时，前端会转为编辑那一期。
    suggested = today if today >= last_date else last_date

    snap = store.get_snapshot(date_text)
    items = []
    if snap is not None:
        for item in store.items_of(int(snap["id"])):
            items.append({
                "account_id": int(item["account_id"]),
                "account": item["account_name"],
                "kind": item["kind"],
                "category": item["category"],
                "subclass": item["subclass"],
                "amount_cny": r2(item["amount_cny"]),
                "amount_usd": item["amount_usd"],
                "fx_rate": r6(item["fx_rate"]),
                "note": item["note"],
                "auto_filled": 1,
            })
    fx_default = snap["default_fx_rate"] if snap is not None else None
    if fx_default is None:
        rates = [i["fx_rate"] for i in items if i["fx_rate"] is not None]
        fx_default = rates[-1] if rates else None
    return {
        "date_default": suggested.isoformat(),
        "fx_rate_default": r6(fx_default),
        "source_date": date_text,
        "items": items,
    }


def _opt_float(value: Any, idx: int, field: str) -> float | None:
    """可选的数值字段：None/空串 → None；非法 → 400。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(400, "bad_request", f"items[{idx}].{field} 非法") from exc


def save_snapshot(store: Store, payload: dict[str, Any], date_from_path: str | None = None) -> dict[str, Any]:
    """POST /api/snapshots 与 PUT /api/snapshots/{date} 共用的 upsert。"""
    date_raw = payload.get("date") or date_from_path
    if not date_raw:
        raise DomainError(400, "bad_request", "缺少 date")
    date_text = normalize_date(date_raw)
    if date_from_path and normalize_date(date_from_path) != date_text:
        raise DomainError(400, "bad_request", "路径 date 与请求体 date 不一致")

    raw_items = payload.get("items")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise DomainError(400, "bad_request", "items 必须是数组")

    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    # 期级默认汇率：明细只给美元、没给行级汇率时用它折算
    fx_default_hint = _opt_float(payload.get("default_fx_rate", payload.get("fx_rate_default")), 0, "default_fx_rate")
    for idx, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise DomainError(400, "bad_request", f"items[{idx}] 必须是对象")
        if raw.get("account_id") is None:
            raise DomainError(400, "bad_request", f"items[{idx}] 缺少 account_id")
        try:
            account_id = int(raw["account_id"])
        except (TypeError, ValueError) as exc:
            raise DomainError(400, "bad_request", f"items[{idx}].account_id 非法") from exc
        account = store.get_account(account_id)
        if account is None:
            raise DomainError(400, "bad_request", f"items[{idx}].account_id 不存在：{account_id}")
        if account_id in seen:
            raise DomainError(400, "bad_request", f"同一期重复的 account_id：{account_id}")
        seen.add(account_id)

        amount_usd = _opt_float(raw.get("amount_usd"), idx, "amount_usd")
        fx_rate_item = _opt_float(raw.get("fx_rate"), idx, "fx_rate")
        if amount_usd is not None and fx_rate_item is None:
            fx_rate_item = fx_default_hint
        raw_cny = raw.get("amount_cny")
        if raw_cny is None or raw_cny == "":
            # 只给美元金额 → 按汇率折算人民币（amount_cny 始终是权威值）。
            # 原表 257/273 个美元行满足 |amount_cny| == amount_usd × fx_rate，
            # 且负债行是「人民币为负、美元为正」，所以按 kind 定符号。
            if amount_usd is None or not fx_rate_item:
                raise DomainError(400, "bad_request",
                                  f"items[{idx}] 缺少 amount_cny（或提供 amount_usd + fx_rate）")
            derived = r2(abs(amount_usd) * fx_rate_item) or 0.0
            amount = -abs(derived) if account["kind"] == "liability" else abs(derived)
        else:
            try:
                amount = float(raw_cny)
            except (TypeError, ValueError) as exc:
                raise DomainError(400, "bad_request", f"items[{idx}].amount_cny 非法") from exc
        items.append({
            "account_id": account_id,
            "amount_cny": amount,
            "amount_usd": amount_usd,
            "fx_rate": fx_rate_item,
            "note": raw.get("note"),
            "auto_filled": 1 if raw.get("auto_filled") else 0,
            "flags": raw.get("flags"),
        })

    status = str(payload.get("status") or "final")
    if status not in {"final", "draft"}:
        raise DomainError(400, "bad_request", f"status 只能是 final/draft：{status}")
    fx = payload.get("default_fx_rate", payload.get("fx_rate_default"))
    if fx is None:
        rates = [i["fx_rate"] for i in items if i["fx_rate"] is not None]
        fx = rates[-1] if rates else None
    ts = now_iso()
    _sid, created = store.upsert_snapshot(
        date_text, items, status=status, default_fx_rate=fx,
        market_note=payload.get("market_note"),
        flags=payload.get("data_quality_flags") if payload.get("data_quality_flags") is not None else None,
        source=str(payload.get("source") or "manual"),
        created_at=ts, updated_at=ts,
    )
    detail = snapshot_detail(store, date_text)
    detail["created"] = created
    return detail


def fx_lookup(store: Store, date_text: str | None, *, refresh: bool = False,
              fetcher: fx.Fetcher | None = None) -> dict[str, Any]:
    """GET /api/fx：某日 USD→CNY 汇率（ECB 中间价为主）。

    抓不到就报 502 —— 绝不返回编造的汇率；人民币金额始终是权威值，汇率只用于折算。
    """
    target = normalize_date(date_text or date_cls.today().isoformat())
    try:
        return fx.resolve(store, target, fetcher=fetcher, refresh=refresh)
    except fx.FxUnavailable as exc:
        raise DomainError(502, "fx_unavailable", f"汇率抓取失败，请手动填写：{exc}") from exc


def delete_snapshot(store: Store, date_text: str) -> dict[str, Any]:
    date_text = normalize_date(date_text)
    if not store.delete_snapshot(date_text):
        raise DomainError(404, "not_found", f"快照不存在：{date_text}")
    return {"ok": True, "date": date_text}


def snapshot_diff(store: Store, date_text: str, against: str | None) -> dict[str, Any]:
    """GET /api/snapshots/{date}/diff?against=YYYY-MM-DD。"""
    date_text = normalize_date(date_text)
    target = store.get_snapshot(date_text)
    if target is None:
        raise DomainError(404, "not_found", f"快照不存在：{date_text}")

    if against:
        against_text = normalize_date(against)
        base_snap = store.get_snapshot(against_text)
        if base_snap is None:
            raise DomainError(404, "not_found", f"对比期不存在：{against_text}")
    else:
        dates = [s["date"] for s in store.snapshot_totals() if str(s["date"]) < date_text]
        against_text = str(dates[-1]) if dates else None
        base_snap = store.get_snapshot(against_text) if against_text else None

    def _map(snap_row: sqlite3.Row | None) -> dict[str, dict[str, Any]]:
        if snap_row is None:
            return {}
        out = {}
        for item in store.items_of(int(snap_row["id"])):
            out[item["account_name"]] = {
                "amount": r2(item["amount_cny"]) or 0.0,
                "account_id": int(item["account_id"]),
                "kind": item["kind"],
            }
        return out

    before_map = _map(base_snap)
    after_map = _map(target)
    names = sorted(set(before_map) | set(after_map), key=lambda n: (after_map.get(n, before_map.get(n, {})).get("account_id") or 0, n))
    rows = []
    b_total = a_total = 0.0
    for name in names:
        info = after_map.get(name) or before_map.get(name) or {}
        before = float(before_map.get(name, {}).get("amount", 0.0))
        after = float(after_map.get(name, {}).get("amount", 0.0))
        delta = after - before
        b_total += before
        a_total += after
        rows.append({
            "account_id": info.get("account_id"),
            "account": name,
            "kind": info.get("kind"),
            "before": r2(before),
            "after": r2(after),
            "delta": r2(delta),
            "pct": None if abs(before) < MONEY_EPS else r2(delta / before * 100.0),
        })
    return {
        "date": date_text,
        "against": against_text,
        "rows": rows,
        "totals": {"before": r2(b_total), "after": r2(a_total), "delta": r2(a_total - b_total)},
    }


# ---------------------------------------------------------------- 指标

def networth_metrics(store: Store) -> dict[str, Any]:
    """GET /api/metrics/networth。"""
    periods = store.snapshot_totals()
    points = []
    prev: float | None = None
    for row in periods:
        networth = r2(row["networth"]) or 0.0
        delta = None if prev is None else r2(networth - prev)
        growth = None
        if prev not in (None, 0.0):
            growth = r2((networth - prev) / abs(prev) * 100.0)
        points.append({
            "date": row["date"],
            "networth": networth,
            "total_assets": r2(row["total_assets"]),
            "total_liabilities": r2(row["total_liabilities"]),
            "delta": delta,
            "growth_pct": growth,
        })
        prev = networth

    kpi: dict[str, Any] = {"current": None, "change_pct": None, "ytd_pct": None,
                           "trailing_12m_pct": None, "trailing_12m_base": None,
                           "usd_share": None, "debt_ratio": None, "periods": len(points)}
    if points:
        last = points[-1]
        kpi["current"] = last["networth"]
        kpi["change_pct"] = last["growth_pct"]
        year = str(last["date"])[:4]
        base = None
        for point in points[:-1]:
            if str(point["date"])[:4] < year:
                base = point["networth"]
        if base not in (None, 0.0):
            kpi["ytd_pct"] = r2((last["networth"] - base) / abs(base) * 100.0)
        elif len(points) > 1:
            first = points[0]["networth"]
            if first:
                kpi["ytd_pct"] = r2((last["networth"] - first) / abs(first) * 100.0)

        # 近 12 个月涨幅：基准 = 目标日期往前一年当日或之前最近的一个点（与 YTD 口径区分开）
        last_date = str(last["date"])
        try:
            y, m, d = (int(part) for part in last_date.split("-"))
            target = f"{y - 1:04d}-{m:02d}-{d:02d}"
            base12: tuple[str, float] | None = None
            for point in points[:-1]:
                if str(point["date"]) <= target and point["networth"]:
                    base12 = (str(point["date"]), float(point["networth"]))
            if base12 is not None and base12[1] != 0.0:
                kpi["trailing_12m_pct"] = r2((last["networth"] - base12[1]) / abs(base12[1]) * 100.0)
                kpi["trailing_12m_base"] = base12[0]
        except (ValueError, TypeError):
            pass

        snap = store.get_snapshot(str(last["date"]))
        if snap is not None:
            items = store.items_of(int(snap["id"]))
            usd_assets = sum(float(i["amount_cny"]) for i in items
                             if i["amount_usd"] is not None and i["kind"] == "asset")
            assets = float(last["total_assets"] or 0.0)
            liabilities = abs(float(last["total_liabilities"] or 0.0))
            kpi["usd_share"] = r2(usd_assets / assets * 100.0) if assets else None
            kpi["debt_ratio"] = r2(liabilities / assets * 100.0) if assets else None
    return {"points": points, "kpi": kpi}


def structure_metrics(store: Store, date_text: str | None) -> dict[str, Any]:
    """GET /api/metrics/structure：资产按大类分组，负债按账户列出（绝对值 + 占比）。"""
    snaps = store.snapshot_totals()
    if not snaps:
        raise DomainError(404, "not_found", "库中还没有快照")
    if date_text:
        date_text = normalize_date(date_text)
        row = next((s for s in snaps if s["date"] == date_text), None)
        if row is None:
            raise DomainError(404, "not_found", f"快照不存在：{date_text}")
    else:
        row = snaps[-1]
    snap = store.get_snapshot(str(row["date"]))
    items = store.items_of(int(snap["id"])) if snap is not None else []

    totals_assets = sum(float(i["amount_cny"]) for i in items if i["kind"] == "asset")
    totals_liab = abs(sum(float(i["amount_cny"]) for i in items if i["kind"] == "liability"))

    groups_map: dict[str, float] = {}
    for item in items:
        if item["kind"] != "asset":
            continue
        # 按小类分组（现金与现金等价物 / 投资类），小类缺失才回退到大类
        key = item["subclass"] or item["category"] or "未分类"
        groups_map[key] = groups_map.get(key, 0.0) + float(item["amount_cny"])
    groups = [
        {"name": name, "amount": r2(amount),
         "share": r2(amount / totals_assets * 100.0) if totals_assets else None}
        for name, amount in sorted(groups_map.items(), key=lambda kv: -abs(kv[1]))
    ]

    liabs_map: dict[str, float] = {}
    liabs_by_account: dict[str, float] = {}
    for item in items:
        if item["kind"] != "liability":
            continue
        amount = abs(float(item["amount_cny"]))
        # 原表负债块没有小类标签，M0 已按科目名合成（信用卡/消费贷/交易所借币/私人往来）
        key = item["subclass"] or item["account_name"]
        liabs_map[key] = liabs_map.get(key, 0.0) + amount
        liabs_by_account[item["account_name"]] = liabs_by_account.get(item["account_name"], 0.0) + amount
    liabilities = [
        {"name": name, "amount": r2(amount),
         "share": r2(amount / totals_liab * 100.0) if totals_liab else None}
        for name, amount in sorted(liabs_map.items(), key=lambda kv: -kv[1])
    ]
    liability_accounts = [
        {"name": name, "amount": r2(amount),
         "share": r2(amount / totals_liab * 100.0) if totals_liab else None}
        for name, amount in sorted(liabs_by_account.items(), key=lambda kv: -kv[1])
    ]
    return {"date": row["date"], "groups": groups, "liabilities": liabilities,
            "liability_accounts": liability_accounts,
            "total_assets": r2(totals_assets), "total_liabilities": r2(-totals_liab),
            "networth": r2(totals_assets - totals_liab)}


def reconcile_metrics(store: Store, settings: Settings) -> dict[str, Any]:
    """GET /api/metrics/reconcile：优先读库内 reconcile_period，回退读 data/out/reconcile-imported.json。"""
    rows = store.list_reconcile()
    if rows:
        periods = []
        for row in rows:
            periods.append({
                "date": row["period_date"],
                "imported": r2(row["imported"]),
                "computed": r2(row["computed"]),
                "diff": r2(row["diff"]),
                "item_count": row["item_count"],
                "extra_table": bool(row["extra_table"]),
                "flags": _flags_of(row["flags"]),
            })
        return {"periods": periods, "source": "db"}

    path = settings.m0_out / "reconcile-imported.json"
    if not path.exists():
        path = settings.m0_out / "reconcile.json"
    if not path.exists():
        raise DomainError(404, "not_found", f"没有对账数据：{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    periods = []
    for row in raw:
        periods.append({
            "date": row.get("date"),
            "imported": r2(row.get("overview")),
            "computed": r2(row.get("computed")),
            "diff": r2(row.get("diff_overview")),
            "item_count": row.get("items"),
            "extra_table": bool(row.get("extra_table")),
            "flags": row.get("flags") or [],
        })
    return {"periods": periods, "source": str(path)}


# ---------------------------------------------------------------- 导入导出

def export_payload(store: Store) -> dict[str, Any]:
    """GET /api/export。"""
    aliases = store.aliases_by_account()
    accounts = []
    for row in store.list_accounts():
        aid = int(row["id"])
        accounts.append({
            "id": aid,
            "name": row["name"],
            "category": row["category"],
            "subclass": row["subclass"],
            "kind": row["kind"],
            "currency": row["currency"],
            "is_liquid": int(row["is_liquid"] or 0),
            "is_counted": int(row["is_counted"] or 0),
            "sort": int(row["sort"] or 0),
            "active_from": row["active_from"],
            "active_to": row["active_to"],
            "note": row["note"],
            "aliases": aliases.get(aid, []),
        })
    name_by_id = {int(r["id"]): r["name"] for r in store.list_accounts()}
    snapshots = []
    for row in store.list_snapshots():
        sid = int(row["id"])
        items = []
        for item in store.items_of(sid):
            items.append({
                "account_id": int(item["account_id"]),
                "account": item["account_name"],
                "kind": item["kind"],
                "amount_cny": r2(item["amount_cny"]),
                "amount_usd": item["amount_usd"],
                "fx_rate": r6(item["fx_rate"]),
                "note": item["note"],
                "auto_filled": int(item["auto_filled"] or 0),
                "flags": _flags_of(item["flags"]),
            })
        snapshots.append({
            "date": row["snapshot_date"],
            "status": row["status"] or "final",
            "default_fx_rate": r6(row["default_fx_rate"]),
            "market_note": row["market_note"],
            "data_quality_flags": _flags_of(row["data_quality_flags"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "items": items,
        })
    events = [{
        "event_date": row["event_date"], "kind": row["kind"],
        "amount_cny": r2(row["amount_cny"]), "note": row["note"],
        "created_at": row["created_at"],
    } for row in store.list_events()]
    return {
        "version": 1,
        "exported_at": now_iso(),
        "accounts": accounts,
        "snapshots": snapshots,
        "events": events,
        "counts": {"accounts": len(accounts), "snapshots": len(snapshots),
                   "items": sum(len(s["items"]) for s in snapshots), "events": len(events)},
    }


def import_payload(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /api/import：与 /api/export 同构，按 name / date 幂等 upsert。"""
    if not isinstance(payload, dict):
        raise DomainError(400, "bad_request", "请求体必须是 JSON 对象")
    accounts = payload.get("accounts") or []
    snapshots = payload.get("snapshots") or []
    events = payload.get("events") or []
    for name, value in (("accounts", accounts), ("snapshots", snapshots), ("events", events)):
        if not isinstance(value, list):
            raise DomainError(400, "bad_request", f"{name} 必须是数组")

    # 1) 账户与别名
    for acc in accounts:
        if not isinstance(acc, dict) or not acc.get("name"):
            raise DomainError(400, "bad_request", "accounts 元素必须包含 name")
        kind = str(acc.get("kind") or "asset")
        if kind not in {"asset", "liability"}:
            raise DomainError(400, "bad_request", f"kind 非法：{kind}")
        fields = {
            "category": acc.get("category"), "subclass": acc.get("subclass"),
            "currency": acc.get("currency") or "CNY",
            "is_liquid": 1 if acc.get("is_liquid") else 0,
            "is_counted": 0 if acc.get("is_counted") in (0, False, "0") else 1,
            "sort": int(acc.get("sort") or 0),
            "active_from": acc.get("active_from"), "active_to": acc.get("active_to"),
            "note": acc.get("note"),
        }
        account_id = store.upsert_account(str(acc["name"]), kind, fields)
        for alias in acc.get("aliases") or []:
            try:
                store.add_alias(account_id, str(alias))
            except ConflictError:
                pass  # 别名已存在于该账户或其他账户时跳过，保持幂等
    name_to_id = {r["name"]: int(r["id"]) for r in store.list_accounts()}

    # 2) 快照与明细
    item_count = 0
    for snap in snapshots:
        if not isinstance(snap, dict) or not snap.get("date"):
            raise DomainError(400, "bad_request", "snapshots 元素必须包含 date")
        date_text = normalize_date(snap["date"])
        items = []
        for raw in snap.get("items") or []:
            if not isinstance(raw, dict):
                raise DomainError(400, "bad_request", "snapshot.items 元素必须是对象")
            account_id = None
            if raw.get("account"):
                account_id = name_to_id.get(str(raw["account"]))
            if account_id is None and raw.get("account_id") is not None:
                row = store.get_account(int(raw["account_id"]))
                if row is not None:
                    account_id = int(row["id"])
            if account_id is None:
                raise DomainError(400, "bad_request",
                                  f"快照 {date_text} 存在未知账户：{raw.get('account') or raw.get('account_id')}")
            flags = _flags_of(raw.get("flags")) if raw.get("flags") else None
            items.append({
                "account_id": account_id,
                "amount_cny": float(raw.get("amount_cny") or 0.0),
                "amount_usd": raw.get("amount_usd"),
                "fx_rate": raw.get("fx_rate"),
                "note": raw.get("note"),
                "auto_filled": 1 if raw.get("auto_filled") else 0,
                "flags": _json_text(flags),
            })
        ts = now_iso()
        store.upsert_snapshot(
            date_text, items,
            status=str(snap.get("status") or "final"),
            default_fx_rate=snap.get("default_fx_rate"),
            market_note=snap.get("market_note"),
            flags=snap.get("data_quality_flags") or [],
            source=str(snap.get("source") or "manual"),
            created_at=snap.get("created_at") or ts,
            updated_at=ts,
        )
        item_count += len(items)

    # 3) 事件
    event_count = 0
    for ev in events:
        if not isinstance(ev, dict) or not ev.get("event_date"):
            continue
        if store.add_event(normalize_date(ev["event_date"]), str(ev.get("kind") or "note"),
                           float(ev.get("amount_cny") or 0.0), ev.get("note"),
                           ev.get("created_at") or now_iso()):
            event_count += 1

    counts = store.counts()
    return {"ok": True, "imported": {"accounts": len(accounts), "snapshots": len(snapshots),
                                     "items": item_count, "events": event_count},
            "counts": counts}


def _json_text(flags: Any) -> str | None:
    """明细行 flags -> JSON 文本（None 保持 None）。"""
    if not flags:
        return None
    return json.dumps(list(flags), ensure_ascii=False)
