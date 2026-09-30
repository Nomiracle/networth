"""networth 后端入口：启动 ThreadingHTTPServer（默认 127.0.0.1:8972）。

    python3 -m app.server [--db PATH] [--host 127.0.0.1] [--port 8972]
    ./run.sh                # 读取环境变量后启动

配置见 app/config.py（NETWORTH_DB / NETWORTH_PORT / NETWORTH_BIND / NETWORTH_SECRET …）。
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from . import api, config, domain
from .store import Store


def main(argv: list[str] | None = None) -> int:
    settings = config.load()
    parser = argparse.ArgumentParser(prog="python3 -m app.server", description="networth 净值管家后端")
    parser.add_argument("--db", default=str(settings.db_path), help="SQLite 路径（默认 NETWORTH_DB）")
    parser.add_argument("--host", default=settings.bind, help="监听地址（默认 NETWORTH_BIND=127.0.0.1）")
    parser.add_argument("--port", type=int, default=settings.port, help="监听端口（默认 NETWORTH_PORT=8972）")
    parser.add_argument("--verbose", action="store_true", help="打印访问日志")
    args = parser.parse_args(argv)

    if args.db != str(settings.db_path) or args.port != settings.port or args.host != settings.bind:
        db_path = Path(args.db).expanduser()
        if not db_path.is_absolute():
            db_path = (Path.cwd() / db_path).resolve()
        settings = replace(settings, db_path=db_path, port=args.port, bind=args.host)

    config.ensure_db_dir(settings)
    store = Store(settings.db_path)
    server = api.build_server(settings, store, host=args.host, port=args.port, verbose=args.verbose)
    host, port = server.server_address[0], server.server_address[1]
    counts = store.counts()
    print(f"[networth] 数据库：{settings.db_path}")
    print(f"[networth] 监听：http://{host}:{port}  （账户 {counts['account']} 个 / 快照 {counts['snapshot']} 期）")
    print(f"[networth] 注册开放：{'是' if settings.allow_registration else '仅首个账号'}；"
          f"前端目录：{settings.web_dist if settings.web_dist.exists() else '（未构建）'}")
    if counts["user"] and domain.secret_mismatch(store, settings):
        print("[networth] ⚠️  NETWORTH_SECRET 与库中记录的指纹不一致：已有账号将无法登录！"
              "请恢复原密钥（.env 会随每日备份一起保存）。", file=sys.stderr)

    def _shutdown(signum: int, frame: object) -> None:
        print(f"\n[networth] 收到信号 {signum}，正在关闭…", file=sys.stderr)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:  # 兜底
        pass
    finally:
        server.server_close()
        store.checkpoint()   # 让停止后的 .db 是完整单文件（避免 cp 漏掉 -wal 丢数据）
        store.close()
        print("[networth] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
