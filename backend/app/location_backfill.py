"""历史地点回填：从 snapshot_item.note 解析「地点：XXX」写法。

命令：
    python3 -m app.location_backfill [--db PATH] [--apply]
默认 dry-run 只报告；--apply 只补 location_text IS NULL 的期，且同期出现不同值时冲突不写。

纪律：
- 不得改动 note 原文一个字符
- 同期多账户同值只记一条；不同值→冲突，列入清单，不写库
- 已有地点的不覆盖
- 幂等：第二次 --apply 必须 0 变更
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from .store import Store

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "networth.db"

# 解析「地点」后跟冒号再跟文本：支持中文冒号/英文冒号，允许前后空白
# 取首行匹配的文本，去空白后非空
LOC_RE = re.compile(r"地点\s*[:：]\s*([^\n\r]+)")

def _parse_note(note: str | None) -> str | None:
    """从单条备注解析地点，返回去空白后的文本或 None。"""
    if not note:
        return None
    m = LOC_RE.search(note)
    if not m:
        return None
    text = m.group(1).strip()
    # 去掉尾随的标点/空白？保留原文城市名，去首尾空白即可
    # 若含多个词，取首个非空片段的整行（测试用合成城市名）
    if not text:
        return None
    # 截取到首个换行或标点前？实际地名就是简单词，如 "示例城"
    # 为了稳健，只取前 30 字符，去掉尾随标点
    text = text.strip()
    # 若文本包含额外说明（如 "示例城 出差中"），只取首段连续非空白？
    # 但真实数据是 "地点：示例城" 单独一行，后面换行才是说明，所以 group 已经是该行剩余
    # 这里按整行取即可，再去除尾随标点
    text = text.split()[0] if " " in text and len(text.split()[0]) <= 10 else text
    # 更稳健：若仍含逗号/句号，截断
    for sep in ("，", ",", "。", "；", ";"):
        if sep in text:
            text = text.split(sep)[0].strip()
            break
    return text.strip() if text.strip() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从历史备注回填地点")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    ap.add_argument("--apply", action="store_true", help="实际写入（默认只报告）")
    args = ap.parse_args(argv)

    store = Store(Path(args.db))
    try:
        # 收集所有快照及其备注解析结果
        snaps = store.list_snapshots()
        # 按日期排序已由 list_snapshots 保证
        scanned = len(snaps)
        # 需要逐期扫描 snapshot_item.note
        hits = 0  # 命中期数（至少有一条可解析）
        distribution: Counter[str] = Counter()
        conflicts: list[tuple[str, list[str]]] = []  # (date, distinct_values)
        to_write: list[tuple[str, str]] = []  # (date, text) 仅对空缺且无冲突

        for snap in snaps:
            date_text = str(snap["snapshot_date"])
            # 已有地点的期跳过（只填 IS NULL）
            if snap["location_text"] is not None and str(snap["location_text"]).strip():
                continue
            items = store.items_of(int(snap["id"]))
            parsed: list[str] = []
            for item in items:
                note = item["note"]
                val = _parse_note(note)
                if val:
                    parsed.append(val)
            if not parsed:
                continue
            hits += 1
            distinct = sorted(set(parsed))
            if len(distinct) == 1:
                val = distinct[0]
                distribution[val] += 1
                to_write.append((date_text, val))
            else:
                distribution.update(distinct)
                conflicts.append((date_text, distinct))

        # 输出统计
        print(f"库：{args.db}")
        print(f"扫描期数：{scanned}")
        print(f"命中期数：{hits}")
        if distribution:
            print("取值分布：")
            for text, cnt in distribution.most_common():
                print(f"  {text}: {cnt} 期")
        if conflicts:
            print(f"冲突清单（{len(conflicts)} 期，同期出现不同值，不写库）：")
            for date_text, vals in conflicts:
                print(f"  {date_text}: {', '.join(vals)}")
        else:
            print("冲突清单：无")

        if args.apply:
            written = 0
            for date_text, text in to_write:
                # 只填空缺，且 source=parsed_note
                changed = store.set_snapshot_location(date_text, text, "parsed_note", None, None, None, None)
                written += changed
            print(f"写入数：{written}")
            # 验证幂等：二次写入应为 0（已在本次写入后，下一次再跑会是 0）
        else:
            print(f"待写入数：{len(to_write)}（加 --apply 才会写入）")
            print("（未写库：dry-run）")
        return 0
    finally:
        store.checkpoint()
        store.close()


if __name__ == "__main__":
    sys.exit(main())
