#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成前端 dev-only mock 数据（src/mock/fixtures.json）——演示数据驱动版本。

为什么不读 data/out：
    仓库曾因真实财务数据外泄被重写为脱敏血缘，公开仓库不再包含任何
    明文数据文件，也不含 data/out。真实数据产物（data/out/*.json）
    永不入库、永不作为 fixture 来源。本脚本唯一数据来源是仓库自带的
    合成生成器 tools/seed_demo.py::generate_dataset(tmpdir, periods, seed)，
    以固定随机种子生成可复现的演示数据，默认即安全，适合直接提交与构建。

唯一数据来源：
    tools/seed_demo.py 的 generate_dataset(tmpdir, periods=24, seed=20260101)
    仅写 accounts.json / snapshots.json / reconcile.json / adjustments.json
    （没有 overview.json）。account 记录中只有
    account/kind/category/subclass/aliases/first_seen/last_seen 等字段，
    没有 last_amount/periods/unchanged_tail/max_amount/min_amount，
    需从 snapshots.json 自行聚合计算。

产物：
    固定写 frontend/src/mock/fixtures.json，payload 含
    generated_at / source="demo" / accounts / snapshots / events / reconcile，
    由 src/mock/index.ts 的 axios adapter 在 DEV 环境动态引入，
    生产构建时该分支被常量折叠摇掉。

用法：
    python3 frontend/scripts/gen_mock_fixtures.py
    python3 frontend/scripts/gen_mock_fixtures.py --periods 24 --seed 20260101
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRONTEND = HERE.parent
ROOT = FRONTEND.parent
OUT = FRONTEND / "src" / "mock" / "fixtures.json"

# 加入仓库根的 tools / backend 到路径，便于 import seed_demo
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from seed_demo import TEMPLATE_ACCOUNT, generate_dataset  # noqa: E402

SUBTOTAL_MARKERS = ("小计", "总计", "净资产")
DEFAULT_PERIODS = 24
DEFAULT_SEED = 20260101


def main() -> int:
    ap = argparse.ArgumentParser(description="生成演示 fixtures.json（合成数据，可提交）")
    ap.add_argument("--periods", type=int, default=DEFAULT_PERIODS, help="期次数量（≥12）")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子")
    args = ap.parse_args()

    if args.periods < 12:
        print("periods 至少为 12", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="networth-fixtures-"))
    try:
        generate_dataset(tmp, periods=args.periods, seed=args.seed)

        accounts_raw = json.loads((tmp / "accounts.json").read_text(encoding="utf-8"))["accounts"]
        snapshots_raw = json.loads((tmp / "snapshots.json").read_text(encoding="utf-8"))
        reconcile_raw = json.loads((tmp / "reconcile.json").read_text(encoding="utf-8"))

        # 账户台账：先编号，is_counted 标记模板行
        accounts = []
        by_name: dict[str, dict] = {}
        for idx, a in enumerate(accounts_raw, start=1):
            name = a["account"]
            rec = {
                "id": idx,
                "name": name,
                "category": a.get("category"),
                "subclass": a.get("subclass"),
                "kind": a.get("kind") or "asset",
                "aliases": a.get("aliases") or [name],
                "first_seen": a.get("first_seen"),
                "last_seen": a.get("last_seen"),
                # 以下字段需从 snapshots 聚合，稍后回填
                "last_amount": None,
                "last_date": None,
                "periods": 0,
                "unchanged_tail": 0,
                "max_amount": None,
                "min_amount": None,
                "is_counted": 0 if TEMPLATE_ACCOUNT in name else 1,
            }
            accounts.append(rec)
            by_name[name] = rec

        # 快照：过滤未映射与小计行，映射 account_id，保留合成字段
        snapshots = []
        dropped = 0
        # 用于后续统计
        per_account: dict[str, list[tuple[str, float]]] = defaultdict(list)

        for snap in snapshots_raw:
            date = snap.get("date")
            if not date:
                continue
            items = []
            for it in snap.get("items") or []:
                acc_name = it.get("account") or ""
                if not it.get("mapped", True):
                    dropped += 1
                    continue
                if any(m in acc_name for m in SUBTOTAL_MARKERS) or acc_name not in by_name:
                    dropped += 1
                    continue
                acc = by_name[acc_name]
                amount = round(float(it.get("amount_cny") or 0), 2)
                items.append({
                    "account_id": acc["id"],
                    "account": acc_name,
                    "kind": acc["kind"],
                    "category": it.get("category") or acc["category"],
                    "subclass": it.get("subclass") or acc["subclass"],
                    "amount_cny": amount,
                    "amount_usd": it.get("amount_usd"),
                    "fx_rate": it.get("fx_rate"),
                    "note": it.get("note"),
                    "auto_filled": 0,
                })
                per_account[acc_name].append((date, amount))
            if not items:
                continue
            snapshots.append({
                "date": date,
                "status": "final",
                "default_fx_rate": 6.9,
                "market_note": snap.get("sheet_note"),
                "data_quality_flags": ("extra_table" if (snap.get("layout") or {}).get("extra_table") else None),
                "items": items,
            })

        snapshots.sort(key=lambda s: s["date"])

        # 回填账户统计（periods/last_amount/last_date/unchanged_tail/max/min/first_seen/last_seen）
        for rec in accounts:
            lst = per_account.get(rec["name"]) or []
            if not lst:
                rec["first_seen"] = None
                rec["last_seen"] = None
                rec["last_amount"] = None
                rec["last_date"] = None
                rec["periods"] = 0
                rec["unchanged_tail"] = 0
                rec["max_amount"] = None
                rec["min_amount"] = None
                continue
            lst.sort(key=lambda x: x[0])
            amounts = [v for _, v in lst]
            rec["first_seen"] = lst[0][0]
            rec["last_seen"] = lst[-1][0]
            rec["last_amount"] = lst[-1][1]
            rec["last_date"] = lst[-1][0]
            rec["periods"] = len(lst)
            rec["max_amount"] = max(amounts) if amounts else None
            rec["min_amount"] = min(amounts) if amounts else None
            # 末尾金额未变的期数
            tail = 0
            for i in range(len(lst) - 1, 0, -1):
                if lst[i][1] == lst[i - 1][1]:
                    tail += 1
                else:
                    break
            rec["unchanged_tail"] = tail

        # reconcile：由 reconcile.json 推导
        reconcile = []
        for r in reconcile_raw:
            reconcile.append({
                "date": r["date"],
                "imported": r.get("overview"),
                "computed": round(float(r.get("computed") or 0), 2),
                "diff": r.get("diff_overview"),
                "flags": r.get("flags") or [],
            })
        reconcile.sort(key=lambda x: x["date"])

        events: list[dict] = []

        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": "demo",
            "accounts": accounts,
            "snapshots": snapshots,
            "events": events,
            "reconcile": reconcile,
        }

        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

        totals = {s["date"]: round(sum(i["amount_cny"] for i in s["items"]), 2) for s in snapshots}
        from collections import Counter
        flags = Counter(i["kind"] for s in snapshots for i in s["items"])
        print(f"accounts={len(accounts)} snapshots={len(snapshots)} events={len(events)} dropped_rows={dropped} reconcile={len(reconcile)}")
        if snapshots:
            print(f"last date={snapshots[-1]['date']} networth={totals[snapshots[-1]['date']]}")
        print(f"items by kind={dict(flags)}")
        print(f"written {OUT} ({os.path.getsize(OUT)} bytes) source=demo periods={args.periods} seed={args.seed}")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
