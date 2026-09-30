#!/usr/bin/env python3
"""networth 演示数据种子：生成合成数据 → 导入 SQLite → 创建演示用户。

设计原则：**仓库不携带任何数据文件**。所有演示数据都由本脚本按固定随机种子
在运行时生成，只存在于本地 SQLite；生成过程中不读取任何外部数据文件。

生成的数据集自洽（服务端重算 == 对账表 computed），并刻意包含几处"脏数据"
以覆盖导入器的分支：
  * 一期出现同账户同额重复行  → 触发「同期同账户多行已合并 / 同额多行」标记
  * 一期负债行金额为正        → 触发 sign_anomaly（保留原符号，不取绝对值）
  * 一期含附加表              → 触发 extra_table
  * 一期总览无记录            → imported/diff 为 null
  * 一期总览与修正值不一致    → 计入 adjusted（不算异常）
  * 一期多账户共用同一段备注  → 投资日志聚合成"期级日志"

用法：
    python3 tools/seed_demo.py --db backend/data/networth.db
    python3 tools/seed_demo.py --db /tmp/demo.db --periods 12 --user demo
    python3 tools/seed_demo.py --source-dir /tmp/ds --db /tmp/demo.db   # 保留生成的 JSON

仅使用 Python 标准库。
"""
from __future__ import annotations

import argparse
import json
import random
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

# backend/ 加入 import 路径，便于直接以脚本方式运行
ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import config, domain, importer  # noqa: E402
from app.store import Store  # noqa: E402

# 账户计划：(前缀, kind, category, subclass, 数量)
PLAN: tuple[tuple[str, str, str, str | None, int], ...] = (
    ("投资账户", "asset", "金融资产", "证券投资", 12),
    ("银行账户", "asset", "现金", "活期存款", 6),
    ("信用卡", "liability", "负债", "信用卡", 3),
    ("借币账户", "liability", "负债", "借币", 2),
    ("私人借款", "liability", "负债", "私人借款", 1),
)
# 模板行：导入但默认不计入统计（is_counted=0）
TEMPLATE_ACCOUNT = "理财/基金（模板行）"

JOURNAL_NOTE = "示例投资日志：本期复盘（合成数据）"
SINGLE_NOTE = "示例账户备注（合成数据）"
DEFAULT_PERIODS = 24
DEFAULT_SEED = 20260101


def _dates(count: int) -> list[str]:
    """生成 count 个连续的月度日期（每月 15 日）。"""
    out: list[str] = []
    year, month = 2024, 1
    for _ in range(count):
        out.append(f"{year:04d}-{month:02d}-15")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def _accounts(invest_count: int) -> list[dict]:
    """构造账户台账（名称全部为通用合成名）。"""
    out: list[dict] = []
    for prefix, kind, category, subclass, count in PLAN:
        total = invest_count if prefix == "投资账户" else count
        for i in range(1, total + 1):
            name = f"{prefix}{i:02d}"
            aliases = [f"{name}·别名1"]
            if i <= 2:
                aliases.append(f"{name}·别名2")
            out.append({"account": name, "kind": kind, "category": category,
                        "subclass": subclass, "aliases": aliases,
                        "first_seen": None, "last_seen": None})
    out.append({"account": TEMPLATE_ACCOUNT, "kind": "asset", "category": "金融资产",
                "subclass": None, "aliases": [], "first_seen": None, "last_seen": None})
    return out


def generate_dataset(out_dir: Path, *, periods: int = DEFAULT_PERIODS,
                     invest_accounts: int = 12, seed: int = DEFAULT_SEED) -> dict:
    """生成一套合成数据集，写入 out_dir 下的 M0 格式 JSON，返回数据集元信息。

    只写 accounts.json / snapshots.json / reconcile.json / adjustments.json。
    """
    if periods < 12:
        raise SystemExit("periods 至少为 12（需要容纳特殊分支的期次）")
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    accounts = _accounts(invest_accounts)
    tradable = [a for a in accounts if a["account"] != TEMPLATE_ACCOUNT]
    dates = _dates(periods)

    # 每期最多 6 个账户，逐期递增到全部账户
    active_max = len(tradable)

    # 各账户的基准金额（资产为正、负债为负）
    level: dict[str, float] = {}
    for acc in tradable:
        if acc["kind"] == "asset":
            level[acc["account"]] = rng.uniform(30_000.0, 600_000.0)
        else:
            level[acc["account"]] = -rng.uniform(5_000.0, 150_000.0)

    value = dict(level)
    usd_account = tradable[0]["account"]
    first_liability = next(a["account"] for a in tradable if a["kind"] == "liability")

    dup_date = dates[5]
    sign_date = dates[9]
    extra_date = dates[3]
    no_overview_date = dates[7]
    adjusted_date = dates[10]
    journal_date = dates[12]
    journal_accounts = [a["account"] for a in tradable[1:4]]
    single_note_account = tradable[5]["account"] if len(tradable) > 5 else tradable[-1]["account"]

    periods_payload: list[dict] = []
    reconcile_payload: list[dict] = []

    for index, date_text in enumerate(dates):
        active = tradable[: min(active_max, 6 + 2 * index)]
        items: list[dict] = []
        for acc in active:
            name = acc["account"]
            # 随机游走（幅度 ±5%），保持同号
            value[name] = value[name] * (1.0 + rng.uniform(-0.05, 0.05))
            amount = round(value[name], 2)
            item = {"account": name, "amount_cny": amount, "kind": acc["kind"],
                    "amount_usd": None, "fx_rate": None, "note": None,
                    "sign_anomaly": False}
            if name == usd_account and index % 2 == 0:
                rate = 7.10 if index % 3 else 7.05
                item["fx_rate"] = rate
                item["amount_usd"] = round(abs(amount) / rate, 2)
            if date_text == journal_date and name in journal_accounts:
                item["note"] = JOURNAL_NOTE
            elif date_text == journal_date and name == single_note_account:
                item["note"] = SINGLE_NOTE
            items.append(item)

        # ① 同账户同额重复行（导入器按账户合并并标记）
        if date_text == dup_date:
            twin = dict(items[0])
            items.append(twin)
            items.sort(key=lambda i: i["account"])

        # ② 负债正号行（保留原符号 + sign_anomaly）
        if date_text == sign_date:
            for item in items:
                if item["account"] == first_liability:
                    item["amount_cny"] = round(abs(item["amount_cny"]) * 0.2, 2)
                    item["sign_anomaly"] = True

        computed = round(sum(i["amount_cny"] for i in items), 2)
        if date_text == no_overview_date:
            overview = None
        elif date_text == adjusted_date:
            overview = round(computed + 500.0, 2)
        else:
            overview = computed
        diff = None if overview is None else round(overview - computed, 2)

        periods_payload.append({
            "ok": True,
            "date": date_text,
            "sheet_note": None,
            "layout": {"extra_table": date_text == extra_date},
            "items": items,
        })
        reconcile_payload.append({
            "date": date_text,
            "status": "OK",
            "overview": overview,
            "computed": computed,
            "diff_overview": diff,
            "items": len(items),
            "extra_table": date_text == extra_date,
            "right_numeric": 0,
            "flags": [],
        })

    (out_dir / "accounts.json").write_text(
        json.dumps({"accounts": accounts}, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "snapshots.json").write_text(
        json.dumps(periods_payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "reconcile.json").write_text(
        json.dumps(reconcile_payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "adjustments.json").write_text(
        json.dumps([{"date": adjusted_date,
                     "reason": "示例：用户确认的历史修正（合成数据）"}],
                   ensure_ascii=False, indent=1), encoding="utf-8")

    return {
        "dates": dates,
        "accounts": accounts,
        "account_count": len(accounts),
        "period_count": len(dates),
        "item_count": sum(len(p["items"]) for p in periods_payload),
        # 重复行那期有 1 行会被导入器按账户合并 → 入库明细比原始行少 1
        "db_item_count": sum(len(p["items"]) for p in periods_payload) - 1,
        "computed": {r["date"]: r["computed"] for r in reconcile_payload},
        "overview": {r["date"]: r["overview"] for r in reconcile_payload},
        "items_per_period": {r["date"]: r["items"] for r in reconcile_payload},
        "dup_date": dup_date,
        "sign_date": sign_date,
        "extra_date": extra_date,
        "no_overview_date": no_overview_date,
        "adjusted_date": adjusted_date,
        "journal_date": journal_date,
        "journal_accounts": journal_accounts,
        "template_account": TEMPLATE_ACCOUNT,
        "usd_account": usd_account,
    }


def seed(db_path: str | Path, *, periods: int = DEFAULT_PERIODS, seed_value: int = DEFAULT_SEED,
         invest_accounts: int = 12, username: str = "demo", password: str | None = None,
         source_dir: Path | None = None, reset: bool = False,
         verbose: bool = True) -> dict:
    """生成数据 → 导入 DB → 创建演示用户；返回 {"meta":…, "username":…, "password":…}。"""
    tmp: Path | None = None
    if source_dir is None:
        tmp = Path(tempfile.mkdtemp(prefix="networth-seed-"))
        source = tmp
    else:
        source = Path(source_dir)

    try:
        meta = generate_dataset(source, periods=periods, invest_accounts=invest_accounts,
                                seed=seed_value)
        rc = importer.run(source, db_path, reset=reset, verbose=verbose)
        if rc != 0:
            raise SystemExit(f"导入未通过对账校验（退出码 {rc}）")

        settings = config.load()
        store = Store(db_path)
        try:
            created = False
            if store.user_count() == 0:
                pwd = password or secrets.token_urlsafe(12)
                domain.register(store, settings, username, pwd)
                created = True
            else:
                pwd = password
            counts = store.counts()
        finally:
            store.checkpoint()
            store.close()

        if verbose:
            print(f"[seed] 规模：账户 {meta['account_count']}、期次 {meta['period_count']}、"
                  f"明细 {meta['item_count']}（含 1 期重复行 / 1 期正号负债 / 1 期附加表）")
            print(f"[seed] 库内计数：{counts}")
            if created:
                print(f"[seed] 演示账号已创建：{username}")
                print(f"[seed] 演示口令：{pwd}")
                print("[seed] 首个账号创建后注册自动关闭；口令请登录后在设置页修改。")
            else:
                print("[seed] 库中已有用户，未创建新账号。")
        return {"meta": meta, "username": username, "password": pwd if created else None}
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    settings = config.load()
    parser = argparse.ArgumentParser(
        prog="python3 tools/seed_demo.py",
        description="生成合成演示数据并写入 SQLite（仓库不含任何数据文件）")
    parser.add_argument("--db", default=str(settings.db_path),
                        help="SQLite 路径，默认 NETWORTH_DB 或 backend/data/networth.db")
    parser.add_argument("--periods", type=int, default=DEFAULT_PERIODS, help="期次数量（≥12）")
    parser.add_argument("--invest-accounts", type=int, default=12, help="投资账户数量")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="随机种子（决定数据内容）")
    parser.add_argument("--user", default="demo", help="演示用户名")
    parser.add_argument("--password", default=None, help="演示口令，默认随机生成")
    parser.add_argument("--source-dir", default=None, help="保留生成的 JSON 到该目录（默认用临时目录）")
    parser.add_argument("--reset", action="store_true", help="导入前清空业务表")
    parser.add_argument("--quiet", action="store_true", help="只打印关键信息")
    args = parser.parse_args(argv)

    seed(args.db, periods=args.periods, seed_value=args.seed,
         invest_accounts=args.invest_accounts, username=args.user,
         password=args.password,
         source_dir=Path(args.source_dir) if args.source_dir else None,
         reset=args.reset, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
