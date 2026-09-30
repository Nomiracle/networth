"""历史期次汇率：校对 + 回填空缺（默认只报告，不写库）。

原则与净值项目一致：**用户已填写的值不动**。这个脚本只做两件事：
1. 报告：逐期对比「表内汇率」与「ECB 中间价」，标出偏差（供人工决定是否修正）；
2. 回填：把 default_fx_rate 为空的期补上（需要显式 --apply）。

用法（在 backend 目录下）：
    python3 -m app.fx_backfill              # 只报告
    python3 -m app.fx_backfill --apply      # 回填空缺的期（不动已有值）
    python3 -m app.fx_backfill --offline    # 只用缓存，不联网
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import fx
from .store import Store

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "networth.db"
# 偏差阈值：≤0.5% 视为四舍五入范围；>1% 需要人工看一眼
ROUND_TOLERANCE = 0.5
SUSPICIOUS = 1.0


def _offline(target: str) -> tuple[str, str, float]:
    raise fx.FxUnavailable("离线模式：不联网")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校对/回填历史期次的 USD→CNY 汇率")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--apply", action="store_true", help="把空缺的期级汇率写回库（默认只报告）")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    args = ap.parse_args(argv)

    store = Store(Path(args.db))
    fetcher = _offline if args.offline else None
    missing: list[tuple[str, dict]] = []
    suspicious: list[tuple[str, float, float, float]] = []
    failed: list[str] = []
    try:
        rows = store.snapshot_fx_rows()
        for date, stored in rows:
            try:
                info = fx.resolve(store, date, fetcher=fetcher)
            except fx.FxUnavailable as exc:
                failed.append(f"{date}（{exc}）")
                continue
            if stored is None:
                missing.append((date, info))
                continue
            pct = abs(float(stored) - info["rate"]) / info["rate"] * 100.0
            if pct > SUSPICIOUS:
                suspicious.append((date, float(stored), info["rate"], pct))

        print(f"库：{args.db}")
        print(f"期数：{len(rows)}；其中已填写 {len(rows) - len(missing)} 期，空缺 {len(missing)} 期")
        if missing:
            print("\n== 空缺（可回填）==")
            for date, info in missing:
                note = "（前一交易日）" if info["stale_days"] else ""
                print(f"  {date}  →  {info['rate']}  {info['source_label']} {info['source_date']}{note}")
        if suspicious:
            print("\n== 与 ECB 偏差 >1%（只报告，不自动改）==")
            for date, stored, rate, pct in suspicious:
                print(f"  {date}  表内 {stored:.4f}  ECB {rate:.4f}  偏差 {pct:.2f}%")
        if failed:
            print(f"\n== 抓取失败（{len(failed)} 期，保留原值）==")
            for line in failed:
                print(f"  {line}")

        if args.apply and missing:
            changed = 0
            for date, info in missing:
                changed += store.set_default_fx_rate(date, info["rate"])
            print(f"\n已回填 {changed} 期空缺汇率（已有值未改动）")
        elif missing:
            print("\n（未写库：加 --apply 才会回填空缺的期）")
        return 0
    finally:
        store.checkpoint()
        store.close()


if __name__ == "__main__":
    sys.exit(main())
