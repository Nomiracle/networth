#!/usr/bin/env python3
"""networth SQLite 备份：一致性快照 + gzip + 保留 7 天。

零依赖（标准库 sqlite3 备份 API，不依赖 sqlite3 CLI）。
用法：python3 backup.py [--db PATH] [--dest DIR] [--keep 7]
退出码：0 成功；1 失败。
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = "<PROJECT_ROOT>/backend/data/networth.db"
DEFAULT_DEST = "<BACKUP_DIR>"
# .env 里是 NETWORTH_SECRET；它被当作口令哈希的 pepper，丢了就永远无法登录，
# 所以必须和数据库一起备份（文件名不同、不进 gz 包，单纯一起放）。
DEFAULT_ENV = "<PROJECT_ROOT>/backend/.env"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--env", default=DEFAULT_ENV)
    ap.add_argument("--keep", type=int, default=7)
    args = ap.parse_args()

    src = Path(args.db)
    if not src.is_file():
        print(f"FAIL: db not found: {src}", file=sys.stderr)
        return 1

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = dest / f"networth-{stamp}.db"
    out = dest / f"networth-{stamp}.db.gz"

    # 一致性快照（WAL 下也安全）
    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(tmp)) as d:
        s.backup(d)
    with open(tmp, "rb") as fi, gzip.open(out, "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo)
    tmp.unlink()

    # 保留最近 keep 份
    backups = sorted(dest.glob("networth-*.db.gz"))
    removed = []
    for old in backups[: max(0, len(backups) - args.keep)]:
        old.unlink()
        removed.append(old.name)

    # 密钥文件同步备份（0600）：丢了 .env 就等于所有账号永久无法登录
    env_copied = "-"
    env_src = Path(args.env)
    if env_src.is_file():
        env_out = dest / "networth-env-current"
        shutil.copyfile(env_src, env_out)
        env_out.chmod(0o600)
        env_copied = env_out.name

    with sqlite3.connect(str(src)) as c:
        accounts = c.execute("SELECT COUNT(*) FROM account").fetchone()[0]
        periods = c.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
        items = c.execute("SELECT COUNT(*) FROM snapshot_item").fetchone()[0]
    size = out.stat().st_size / 1024
    print(f"OK {out} ({size:.0f} KB) accounts={accounts} periods={periods} items={items} "
          f"kept={min(len(backups), args.keep)} removed={removed or '-'} env={env_copied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
