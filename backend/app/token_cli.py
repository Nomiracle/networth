"""服务令牌管理 CLI（复用现有 token 表与摘要机制）。

命令：
    python3 -m app.token_cli create --name mcp --days 3650
    python3 -m app.token_cli list
    python3 -m app.token_cli revoke --id N

create 只在标准输出打印一次明文令牌，绝不写入文件或日志。
"""
from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from . import config, domain
from .store import Store


def _get_store() -> Store:
    settings = config.load()
    settings.db_dir.mkdir(parents=True, exist_ok=True)
    return Store(settings.db_path)


def cmd_create(args: argparse.Namespace) -> int:
    name = (args.name or "").strip()
    if not name:
        print("错误：--name 不能为空", file=sys.stderr)
        return 2
    try:
        days = int(args.days)
    except (TypeError, ValueError):
        print("错误：--days 必须是整数", file=sys.stderr)
        return 2
    if days <= 0:
        print("错误：--days 必须大于 0", file=sys.stderr)
        return 2

    settings = config.load()
    # 覆盖 token 有效期
    from dataclasses import replace

    custom_settings = replace(settings, token_ttl_days=days)
    store = Store(custom_settings.db_path)
    try:
        # 复用用户表：按 name 查找或创建
        user = store.get_user_by_name(name)
        if user is None:
            # 创建服务账号，密码随机且不记录明文（仅用于满足 user 表约束）
            raw_pwd = secrets.token_urlsafe(16)
            pwd_hash = domain.hash_password(raw_pwd, custom_settings.secret)
            created_at = domain.now_iso()
            try:
                user_id = store.create_user(name, pwd_hash, created_at)
            except Exception as exc:  # noqa: BLE001
                print(f"创建用户失败: {exc}", file=sys.stderr)
                return 1
            # 记录密钥指纹（若库中为空）
            domain.remember_secret(store, custom_settings)
        else:
            user_id = int(user["id"])

        token_info = domain.create_token(store, custom_settings, user_id)
        # 只打印明文令牌到 stdout，绝不写入文件/日志
        sys.stdout.write(token_info["token"] + "\n")
        sys.stdout.flush()
        # 提示信息走 stderr，不污染 stdout
        print(f"[token_cli] 已为用户 '{name}' 签发令牌，用户 ID {user_id}，过期时间 {token_info['expires_at']}", file=sys.stderr)
        return 0
    finally:
        store.close()


def cmd_list(args: argparse.Namespace) -> int:
    store = _get_store()
    try:
        rows = list(store.conn.execute(
            "SELECT token.id, token.user_id, user.username, token.created_at, token.expires_at, token.revoked_at "
            "FROM token LEFT JOIN user ON user.id = token.user_id ORDER BY token.id"
        ).fetchall())
        if not rows:
            print("暂无令牌", file=sys.stderr)
            return 0
        # 表头
        header = f"{'ID':<6} {'用户':<20} {'创建时间':<20} {'过期时间':<20} {'状态'}"
        print(header, file=sys.stdout)
        print("-" * len(header), file=sys.stdout)
        for row in rows:
            status = "已注销" if row["revoked_at"] else "有效"
            print(f"{row['id']:<6} {row['username'] or row['user_id']:<20} {row['created_at']:<20} {row['expires_at']:<20} {status}", file=sys.stdout)
        return 0
    finally:
        store.close()


def cmd_revoke(args: argparse.Namespace) -> int:
    try:
        token_id = int(args.id)
    except (TypeError, ValueError):
        print("错误：--id 必须是整数", file=sys.stderr)
        return 2
    store = _get_store()
    try:
        row = store.conn.execute("SELECT * FROM token WHERE id = ?", (token_id,)).fetchone()
        if row is None:
            print(f"未找到 ID={token_id} 的令牌", file=sys.stderr)
            return 1
        if row["revoked_at"]:
            print(f"令牌 ID={token_id} 已处于注销状态", file=sys.stderr)
            return 0
        digest = row["token_digest"]
        ok = store.revoke_token(digest, domain.now_iso())
        if ok:
            print(f"已注销令牌 ID={token_id}", file=sys.stderr)
            return 0
        print(f"注销失败 ID={token_id}", file=sys.stderr)
        return 1
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m app.token_cli", description="净值管家服务令牌管理")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="签发令牌")
    p_create.add_argument("--name", required=True, help="用户名/服务名")
    p_create.add_argument("--days", default="3650", help="有效期天数（默认 3650）")

    p_list = sub.add_parser("list", help="列出令牌")
    p_revoke = sub.add_parser("revoke", help="注销令牌")
    p_revoke.add_argument("--id", required=True, help="令牌 ID")

    args = parser.parse_args(argv)
    if args.command == "create":
        return cmd_create(args)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "revoke":
        return cmd_revoke(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
