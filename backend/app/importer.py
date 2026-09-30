"""networth 历史数据导入 CLI（M0 产物 -> SQLite）。

用法：
    python3 -m app.importer --source ../data/out --db /tmp/nw-m1.db

行为（docs/m1-spec.md 第 4 节）：
1. accounts.json  -> account + account_alias（`理财/基金（模板行）` 导入但 is_counted=0）
2. snapshots.json -> 每期一个 snapshot（source='excel'）；同一期同一账户的多行明细按
   (snapshot, account) 合并为一行（M0 已对「同账户同额重复行」去重）；被合并的期写入
   snapshot.data_quality_flags；小计/总计/净资产行不导入；
   data_quality_flags 记录 extra_table / 同额重复行已去重 / 用户确认修正 / 正号行
3. reconcile.json -> 写入库内 reconcile_period 表，并落一份 data/out/reconcile-imported.json
4. adjustments.json -> 用户确认过的历史修正（如同额重复行去重）不算异常，
   单独计入 adjusted
5. 结束打印：accounts=N snapshots=62 items=N adjusted=N mismatched=0，任一期不符时以非零码退出
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import config, domain
from .store import ConflictError, Store

# 模板行：导入但默认不计入统计
TEMPLATE_ACCOUNT = "理财/基金（模板行）"

# 对账判定容差：总览序列本身带分位舍入，超过 1 元的差异才算“不符”
MISMATCH_TOLERANCE = 1.0
# 服务端重算 vs M0 computed 的容差（只允许浮点误差）
RECALC_TOLERANCE = 0.005

FLAG_MERGED = "同期同账户多行已合并"


def _load_json(path: Path) -> Any:
    if not path.exists():
        raise SystemExit(f"缺少文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _is_liquid(name: str, subclass: str | None) -> int:
    """现金类资产标记（启发式，仅用于展示口径）。"""
    if "现金" in (subclass or ""):
        return 1
    if "活期" in name or name in {"现金", "活期存款", "公积金"}:
        return 1
    return 0


def import_accounts(store: Store, payload: dict[str, Any]) -> dict[str, int]:
    """导入账户台账与别名，返回计数。"""
    accounts = payload.get("accounts") or []
    alias_count = 0
    for order, acc in enumerate(accounts):
        name = str(acc["account"])
        kind = acc.get("kind") if acc.get("kind") in ("asset", "liability") else "asset"
        fields = {
            "category": acc.get("category"),
            "subclass": acc.get("subclass"),
            "currency": "CNY",
            "is_liquid": _is_liquid(name, acc.get("subclass")),
            "is_counted": 0 if name == TEMPLATE_ACCOUNT else 1,
            "sort": order,
            "active_from": acc.get("first_seen"),
            "active_to": acc.get("last_seen"),
            "note": None,
        }
        account_id = store.upsert_account(name, kind, fields)
        for alias in acc.get("aliases") or []:
            alias = str(alias)
            if alias == name:
                continue  # 别名与账户名相同则无需入库
            try:
                store.add_alias(account_id, alias)
                alias_count += 1
            except ConflictError:
                pass  # 已存在，幂等跳过
    return {"accounts": len(accounts), "aliases": alias_count}


def merge_period_items(period: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], int]:
    """把一期的原始明细按账户合并；返回 (items, 期级 flags, 被合并的行数)。"""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in period.get("items") or []:
        grouped[str(item["account"])].append(item)

    items: list[dict[str, Any]] = []
    flags: list[str] = []
    merged_rows = 0
    dup_flag: str | None = None
    dup_count = 0

    for account, rows in grouped.items():
        amounts = [float(r["amount_cny"]) for r in rows]
        if len(rows) > 1:
            merged_rows += len(rows) - 1
            # 同额多行 = 同一笔被重复填写（M0 已按用户口径去重；此处仅防御性标记）
            repeated = {amt: n for amt, n in Counter(amounts).items() if n > 1}
            if repeated:
                amt, count = max(repeated.items(), key=lambda kv: kv[1])
                if count > dup_count:
                    dup_count = count
                    dup_flag = f"同额多行:{account}×{count}"
        usd_values = [r["amount_usd"] for r in rows if r.get("amount_usd") is not None]
        rates = [r["fx_rate"] for r in rows if r.get("fx_rate") is not None]
        notes = [str(r["note"]) for r in rows if r.get("note")]
        sign_anomaly = any(r.get("sign_anomaly") for r in rows)
        # 注意：这里刻意 **不做** 逐行四舍五入 —— M0 的口径是 round(Σ原始行, 2)，
        # 若先把合并行各自 round 再相加，4 期（2021-11-29/2022-03-02/2022-07-16/2022-10-24）
        # 会与 M0 computed 差 0.01；展示层（domain.r2）再统一保留两位。
        items.append({
            "account_id": None,             # 由调用方按账户名补齐
            "account": account,
            "amount_cny": sum(amounts),
            "amount_usd": sum(usd_values) if usd_values else None,
            "fx_rate": rates[0] if rates else None,
            "note": " / ".join(dict.fromkeys(notes)) or None,
            "auto_filled": 0,
            "flags": ["sign_anomaly"] if sign_anomaly else [],
            "kind": rows[0].get("kind"),
            "sign_anomaly": sign_anomaly,
        })
    if merged_rows:
        flags.append(FLAG_MERGED)
    if dup_flag:
        flags.append(dup_flag)
    items.sort(key=lambda i: i["account"])
    return items, flags, merged_rows


def import_snapshots(store: Store, periods: list[Any], reconcile_rows: dict[str, dict[str, Any]],
                     now: str) -> dict[str, int]:
    """导入全部期次（按 (snapshot, account) 合并明细），返回计数。"""
    name_to_id = {r["name"]: int(r["id"]) for r in store.list_accounts()}
    snapshot_count = item_count = merged_total = 0
    for period in sorted(periods, key=lambda p: p.get("date") or ""):
        if not period.get("ok") or not period.get("date"):
            continue
        date_text = str(period["date"])
        items, flags, merged_rows = merge_period_items(period)
        layout = period.get("layout") or {}
        rec = reconcile_rows.get(date_text) or {}

        # 期级 data_quality_flags：合并 -> 重复计入 -> extra_table -> M0 对账标记（去重保序）
        all_flags = list(flags)
        if layout.get("extra_table"):
            all_flags.append("extra_table")
        for flag in rec.get("flags") or []:
            all_flags.append(str(flag))
        all_flags = list(dict.fromkeys(all_flags))

        rates = [i["fx_rate"] for i in items if i["fx_rate"] is not None]
        default_fx = Counter(rates).most_common(1)[0][0] if rates else None

        payload_items = []
        for item in items:
            account_id = name_to_id.get(str(item["account"]))
            if account_id is None:
                raise SystemExit(f"快照 {date_text} 存在未登记账户：{item['account']}")
            payload_items.append({
                "account_id": account_id,
                "amount_cny": item["amount_cny"],
                "amount_usd": item["amount_usd"],
                "fx_rate": item["fx_rate"],
                "note": item["note"],
                "auto_filled": 0,
                "flags": json.dumps(item["flags"], ensure_ascii=False) if item["flags"] else None,
            })

        store.upsert_snapshot(
            date_text, payload_items,
            status="final",
            default_fx_rate=default_fx,
            market_note=period.get("sheet_note"),
            flags=all_flags,
            source="excel",
            created_at=now, updated_at=now,
        )
        snapshot_count += 1
        item_count += len(payload_items)
        merged_total += merged_rows
    return {"snapshots": snapshot_count, "items": item_count, "merged_rows": merged_total}


def import_reconcile(store: Store, rows: list[Any], source: Path) -> dict[str, int]:
    """把 M0 对账结果写入库内 + data/out/reconcile-imported.json。"""
    normalised = []
    for row in rows:
        if row.get("status") == "PARSE_FAIL":
            continue
        normalised.append({
            "date": row.get("date"),
            "imported": row.get("overview"),
            "computed": row.get("computed"),
            "diff": row.get("diff_overview"),
            "flags": row.get("flags") or [],
            "items": row.get("items"),
            "extra_table": bool(row.get("extra_table")),
            "right_numeric": row.get("right_numeric") or 0,
        })
    store.replace_reconcile(normalised)
    out_path = source / "reconcile-imported.json"
    out_path.write_text(json.dumps(normalised, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"reconcile": len(normalised)}


def verify(store: Store, reconcile_rows: dict[str, dict[str, Any]],
           adjusted: dict[str, str] | None = None, verbose: bool = True) -> dict[str, Any]:
    """校验：① 服务端重算 == M0 computed（全部期）；② 与总览差异 ≤ 容差。

    用户在 M0 里确认过修正的期（data/out/adjustments.json）与总览本就应当不同，
    单独计入 adjusted，不当作异常。
    """
    adjusted = adjusted or {}
    by_date = {row["date"]: row for row in store.snapshot_totals()}
    recalc_bad, overview_bad, adjusted_rows = [], [], []
    for date_text, rec in sorted(reconcile_rows.items()):
        computed = rec.get("computed")
        imported = rec.get("overview")
        row = by_date.get(date_text)
        if row is None:
            recalc_bad.append((date_text, "missing", computed))
            continue
        if computed is not None and abs(float(row["networth"]) - float(computed)) > RECALC_TOLERANCE:
            recalc_bad.append((date_text, row["networth"], computed))
        if imported is not None and abs(float(row["networth"]) - float(imported)) > MISMATCH_TOLERANCE:
            if date_text in adjusted:
                adjusted_rows.append((date_text, row["networth"], imported, adjusted[date_text]))
            else:
                overview_bad.append((date_text, row["networth"], imported))
    with_overview = sum(1 for r in reconcile_rows.values() if r.get("overview") is not None)
    if verbose:
        print(f"[importer] 校验：服务端重算 vs M0 computed 不符 {len(recalc_bad)} 期；"
              f"vs 总览（{with_overview} 期有记录）不符 {len(overview_bad)} 期；"
              f"用户确认修正 {len(adjusted_rows)} 期")
        for date_text, got, want in recalc_bad[:10]:
            print(f"  ! 重算不符 {date_text}: 服务端={got} M0={want}")
        for date_text, got, want in overview_bad[:10]:
            print(f"  ! 总览不符 {date_text}: 服务端={got} 总览={want}")
        for date_text, got, want, reason in adjusted_rows[:10]:
            print(f"  ~ 用户确认修正 {date_text}: 服务端={got} 总览={want}（{reason}）")
    return {"recalc_bad": recalc_bad, "overview_bad": overview_bad, "adjusted": adjusted_rows,
            "periods_with_overview": with_overview, "periods_total": len(reconcile_rows)}


def run(source: Path, db_path: str | Path, reset: bool = False, verbose: bool = True) -> int:
    """执行一次完整导入；返回进程退出码（0 = 全部对账通过）。"""
    settings = config.load()
    store = Store(db_path)
    try:
        if reset:
            with store.tx() as conn:
                for table in ("snapshot_item", "snapshot", "account_alias", "account",
                              "event", "reconcile_period"):
                    conn.execute(f"DELETE FROM {table}")
            if verbose:
                print("[importer] --reset：已清空业务表（user/token 保留）")

        accounts_payload = _load_json(source / "accounts.json")
        periods = _load_json(source / "snapshots.json")
        reconcile_payload = _load_json(source / "reconcile.json")
        reconcile_rows = {str(r["date"]): r for r in reconcile_payload if r.get("date")}

        # 用户确认过的历史修正（M0 写出）：与总览的差异属于预期，不算异常
        adjusted_map: dict[str, str] = {}
        adj_path = source / "adjustments.json"
        if adj_path.exists():
            for adj in json.loads(adj_path.read_text(encoding="utf-8")):
                adjusted_map[str(adj.get("date"))] = str(adj.get("reason") or "用户确认修正")

        now = domain.now_iso()
        acc_stats = import_accounts(store, accounts_payload)
        snap_stats = import_snapshots(store, periods, reconcile_rows, now)
        rec_stats = import_reconcile(store, reconcile_payload, source)
        result = verify(store, reconcile_rows, adjusted_map, verbose=verbose)

        counts = store.counts()
        if verbose:
            print(f"[importer] 源目录：{source}")
            print(f"[importer] DB：{db_path}")
            print(f"[importer] 账户 {acc_stats['accounts']} 个（别名 {acc_stats['aliases']} 条）；"
                  f"快照 {snap_stats['snapshots']} 期（明细 {snap_stats['items']} 行，"
                  f"同期同账户多行合并 {snap_stats['merged_rows']} 行）；"
                  f"对账 {rec_stats['reconcile']} 期")
            print(f"[importer] 库内计数：{counts}")
        mismatched = len(result["recalc_bad"]) + len(result["overview_bad"])
        print(f"accounts={counts['account']} snapshots={counts['snapshot']} "
              f"items={counts['snapshot_item']} adjusted={len(result['adjusted'])} "
              f"mismatched={mismatched}")
        return 0 if mismatched == 0 else 1
    finally:
        # 让 CLI 结束后留下的 .db 是完整单文件（否则 cp 主库会漏掉 -wal 里的数据）
        store.checkpoint()
        store.close()


def main(argv: list[str] | None = None) -> int:
    settings = config.load()
    parser = argparse.ArgumentParser(
        prog="python3 -m app.importer",
        description="把 M0 解析产物（data/out/*.json）幂等导入 SQLite",
    )
    parser.add_argument("--source", default=str(settings.m0_out),
                        help="M0 产物目录，默认 NETWORTH_M0_OUT 或 ../data/out")
    parser.add_argument("--db", default=str(settings.db_path),
                        help="SQLite 路径，默认 NETWORTH_DB 或 backend/data/networth.db")
    parser.add_argument("--reset", action="store_true", help="导入前清空账户/快照/事件/对账表")
    parser.add_argument("--quiet", action="store_true", help="只打印最后一行摘要")
    args = parser.parse_args(argv)
    source = Path(args.source).expanduser()
    if not source.is_absolute():
        source = (Path.cwd() / source).resolve()
    return run(source, args.db, reset=args.reset, verbose=not args.quiet)


if __name__ == "__main__":
    sys.exit(main())
