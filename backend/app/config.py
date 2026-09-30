"""networth 后端配置模块（零第三方依赖，全部使用标准库）。

所有配置项都从环境变量读取，未设置时使用本地默认值（见 docs/m1-spec.md 第 6 节）：

    NETWORTH_DB            数据库路径，默认 backend/data/networth.db
    NETWORTH_SECRET        服务端密钥（密码加盐/派生用），未设置时用本地默认值
    NETWORTH_PORT          监听端口，默认 8972
    NETWORTH_BIND          监听地址，默认 127.0.0.1
    ALLOW_REGISTRATION     1/0，是否允许注册新用户；未设置时仅允许首个账号注册
    NETWORTH_M0_OUT        M0 解析产物目录（accounts/snapshots/reconcile.json），默认 ../data/out
    NETWORTH_WEB_DIST      前端构建产物目录，默认 ../frontend/dist

注意：load() 每次调用都会重新读取环境变量，方便测试在运行期注入临时配置。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# backend/app/config.py -> backend/app -> backend -> 项目根目录
BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent

# 未设置 NETWORTH_SECRET 时使用的本地默认值（仅用于本地开发，生产必须覆盖）
DEFAULT_SECRET = "networth-local-dev-secret-change-me"

# token 有效期（天）
TOKEN_TTL_DAYS = 30


def _env(name: str, default: str | None = None) -> str | None:
    """读取环境变量，空字符串按未设置处理。"""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str) -> bool:
    """把 1/true/yes/on 视为真。"""
    value = (_env(name) or "").lower()
    return value in {"1", "true", "yes", "on"}


def _resolve_path(value: str, base: Path) -> Path:
    """相对路径按 base 解析，绝对路径原样返回。"""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _m0_context() -> Path:
    """NETWORTH_M0_OUT 相对路径的基准目录。

    默认值 ../data/out 是相对 backend/ 的（与文档里 `cd backend && python3 -m app.importer`
    的用法一致）；如果从项目根目录运行，也会尝试 PROJECT_ROOT/data/out。
    """
    return BACKEND_DIR


@dataclass(frozen=True)
class Settings:
    """一次运行期内的全部配置。"""

    db_path: Path
    secret: bytes
    port: int
    bind: str
    allow_registration: bool
    m0_out: Path
    web_dist: Path
    token_ttl_days: int = TOKEN_TTL_DAYS

    @property
    def db_dir(self) -> Path:
        return self.db_path.parent


def _m0_out_default() -> Path:
    """默认 M0 目录：优先 backend/../data/out，其次项目根 data/out。"""
    candidate = (BACKEND_DIR / ".." / "data" / "out").resolve()
    return candidate


def load() -> Settings:
    """按当前环境变量构造配置。"""
    db_raw = _env("NETWORTH_DB") or str(BACKEND_DIR / "data" / "networth.db")
    secret = (_env("NETWORTH_SECRET") or DEFAULT_SECRET).encode("utf-8")

    port_raw = _env("NETWORTH_PORT") or "8972"
    try:
        port = int(port_raw)
    except ValueError as exc:  # 明确的错误信息，避免静默用错端口
        raise SystemExit(f"NETWORTH_PORT 不是合法端口：{port_raw!r}") from exc

    bind = _env("NETWORTH_BIND") or "127.0.0.1"

    m0_raw = _env("NETWORTH_M0_OUT")
    m0_out = _resolve_path(m0_raw, _m0_context()) if m0_raw else _m0_out_default()

    dist_raw = _env("NETWORTH_WEB_DIST")
    web_dist = _resolve_path(dist_raw, _m0_context()) if dist_raw else (BACKEND_DIR / ".." / "frontend" / "dist").resolve()

    return Settings(
        db_path=_resolve_path(db_raw, _m0_context()),
        secret=secret,
        port=port,
        bind=bind,
        allow_registration=_env_bool("ALLOW_REGISTRATION"),
        m0_out=m0_out,
        web_dist=web_dist,
    )


def ensure_db_dir(settings: Settings) -> None:
    """确保数据库目录存在。"""
    settings.db_dir.mkdir(parents=True, exist_ok=True)
