"""USD→CNY 汇率：ECB 中间价为主、open.er-api 兜底，SQLite 缓存。

约定（与净值项目其它部分一致）：
- 汇率只用于「美元金额 → 人民币」折算；**人民币金额始终是权威值**。
- 抓不到就抛错，**绝不返回编造的汇率**（宁可让用户手填）。
- ECB 只在工作日发布，周末/节假日向前取最近一个交易日，并保留真实生效日，
  这样前端可以如实标注「前一交易日」而不是假装是当天。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .store import Store

# ECB 参考汇率（免密钥，含完整历史；实测与原表口径中位偏差 0.119%）
ECB_URL = "https://api.frankfurter.app/{start}..{end}?from=USD&to=CNY"
# 兜底：只有当前值，没有历史
ERAPI_URL = "https://open.er-api.com/v6/latest/USD"

SOURCE_LABELS = {"ecb": "ECB 中间价", "er-api": "open.er-api 参考价"}
# 周末/节假日向前找最近一个交易日的最大跨度
LOOKBACK_DAYS = 10
HTTP_TIMEOUT = 8
# 合理性护栏：USD→CNY 历史上从未离开这个区间；越界说明源返回了垃圾
RATE_MIN, RATE_MAX = 3.0, 15.0

Fetcher = Callable[[str], "tuple[str, str, float]"]


class FxUnavailable(Exception):
    """所有汇率源都不可用，或返回值不可信。"""


def _http_json(url: str, timeout: int = HTTP_TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "networth/1.0 (+personal ledger)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定的 https 地址
        return json.loads(resp.read().decode("utf-8"))


def _check(rate: float, source: str) -> float:
    if not (RATE_MIN <= rate <= RATE_MAX):
        raise FxUnavailable(f"{source} 返回的汇率超出合理区间（{rate}），拒绝使用")
    return rate


def ecb_rate(target: str) -> tuple[str, float]:
    """ECB 参考汇率；返回 (生效日, 汇率)。ECB 只有工作日数据。"""
    end = date_cls.fromisoformat(target)
    start = end - timedelta(days=LOOKBACK_DAYS)
    data = _http_json(ECB_URL.format(start=start.isoformat(), end=end.isoformat()))
    rates = data.get("rates") or {}
    days = sorted(d for d, v in rates.items() if isinstance(v, dict) and v.get("CNY"))
    if not days:
        raise FxUnavailable(f"ECB 在 {start} ~ {end} 没有可用汇率")
    day = days[-1]
    return day, _check(float(rates[day]["CNY"]), "ECB")


def erapi_rate() -> tuple[str, float]:
    """兜底源：只有当前值。返回 (生效日, 汇率)。"""
    data = _http_json(ERAPI_URL)
    rate = (data.get("rates") or {}).get("CNY")
    if not rate:
        raise FxUnavailable("open.er-api 未返回 CNY 汇率")
    ts = data.get("time_last_update_unix")
    day = datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat() if ts else \
        date_cls.today().isoformat()
    return day, _check(float(rate), "open.er-api")


def default_fetcher(target: str) -> tuple[str, str, float]:
    """按回退链抓取，返回 (source, source_date, rate)；全部失败抛 FxUnavailable。"""
    errors = []
    for source, fn in (("ecb", lambda: ecb_rate(target)), ("er-api", erapi_rate)):
        try:
            day, rate = fn()
            return source, day, rate
        except (FxUnavailable, urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            errors.append(f"{SOURCE_LABELS.get(source, source)}: {exc}")
    raise FxUnavailable("；".join(errors) or "没有可用的汇率源")


def resolve(
    store: Store,
    target: str,
    *,
    fetcher: Fetcher | None = None,
    refresh: bool = False,
    now: str | None = None,
) -> dict[str, Any]:
    """取某日 USD→CNY 汇率：先查缓存，必要时抓取并写入缓存。

    `fetcher` 可注入（测试用假抓取器，避免打真实网络）。
    """
    target = date_cls.fromisoformat(target).isoformat()
    if not refresh:
        row = store.get_fx_rate(target)
        if row is not None:
            return _payload(target, row["rate"], row["source"], row["source_date"],
                            row["fetched_at"], cached=True)

    fetch = fetcher or default_fetcher
    try:
        source, source_date, rate = fetch(target)
    except FxUnavailable as exc:
        # 抓取失败时退而用最近一次已知汇率，但如实标注不是当日的值
        last = store.latest_fx_rate(target)
        if last is not None:
            return _payload(target, last["rate"], last["source"], last["source_date"],
                            last["fetched_at"], cached=True, fallback=True)
        raise
    rate = round(float(rate), 4)
    _check(rate, SOURCE_LABELS.get(source, source))  # 注入的抓取器同样要过护栏
    fetched_at = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.upsert_fx_rate(target, rate, source, source_date, fetched_at)
    return _payload(target, rate, source, source_date, fetched_at, cached=False)


def _payload(target: str, rate: float, source: str, source_date: str,
             fetched_at: str, *, cached: bool, fallback: bool = False) -> dict[str, Any]:
    stale_days = (date_cls.fromisoformat(target) - date_cls.fromisoformat(source_date)).days
    return {
        "date": target,
        "rate": round(float(rate), 4),
        "source": source,
        "source_label": SOURCE_LABELS.get(source, source),
        "source_date": source_date,
        "fetched_at": fetched_at,
        "cached": cached,
        # 生效日早于请求日：周末/节假日，或兜底用了更早的值
        "stale_days": max(0, stale_days),
        "fallback": fallback,
    }
