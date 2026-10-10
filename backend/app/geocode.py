"""逆地理编码：坐标粗化 + 缓存 + 限速 + provider 分发。

纪律（沿用 fx.py）：
- 抓取失败绝不编造，直接抛 GeocodeUnavailable
- 已缓存命中则直接返回 cached=true，且不重复出网
- 坐标在出网与写缓存前按约 1km 网格粗化（lat_key/lon_key）
- nominatim 必须带 User-Agent，且限速不超过 1 次/秒
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from .store import Store

# 粗化：约 1km ≈ 0.009° 纬度，取 0.01° 网格（四舍五入到 2 位小数）
GRID = 0.01

Fetcher = Callable[[float, float], tuple[str, str]]  # (lat, lon) -> (text, raw_json_str)

_last_fetch_lock = threading.Lock()
_last_fetch_ts = 0.0


class GeocodeUnavailable(Exception):
    """抓取或解析失败。"""


def quantize(lat: float, lon: float) -> tuple[str, str]:
    """把坐标粗化到约 1km 网格，返回 (lat_key, lon_key) 字符串。

    使用 round 到 2 位小数（0.01° ≈ 1.11km），保证同一网格内的不同坐标
    命中同一缓存键，且出网前已做隐私粗化。
    """
    # 使用 round 保持与测试的确定性一致
    lat_q = round(float(lat) / GRID) * GRID
    lon_q = round(float(lon) / GRID) * GRID
    # 格式化为固定小数，避免浮点表示差异导致键不命中
    return f"{lat_q:.2f}", f"{lon_q:.2f}"


def _throttle():
    """nominatim 限速：全局不超过 1 次/秒。"""
    global _last_fetch_ts
    with _last_fetch_lock:
        now = time.monotonic()
        elapsed = now - _last_fetch_ts
        if _last_fetch_ts != 0 and elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _last_fetch_ts = time.monotonic()


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: int = 8) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 受控 URL
        return resp.read().decode("utf-8")


def _strip_city_suffix(name: str) -> str:
    """去掉地名末尾的行政后缀「市」，与用户既有写法对齐。

    为什么值得单独做：本服务历史期次里的地点是用户手填的「示例城甲 / 示例城乙」（不带「市」），
    而 provider 返回的是「示例城甲市 / 示例城乙市」。若原样写入，同一个城市会在
    `/api/metrics/locations` 里裂成两组、也不好与「沿用上期」的值比较。只剥一个「市」字，
    不改其它任何字（外文地名不含该字，不受影响）。
    """
    text = (name or "").strip()
    if len(text) > 1 and text.endswith("市"):
        return text[:-1]
    return text


def _city_level(addr: dict[str, Any], display_name: str = "") -> str:
    """从 provider 返回里取城市级名称（本仓库地点字段是城市粒度）。

    2026-10-10 实测（公开坐标）：Nominatim 对中文地址会把「区」放进 city（示例：address 里只有
    city=示例区 与 postcode，没有任何市名），只有 display_name 才带市名；地级市有时连市名也不在
    address 里。因此按「名字是否以『市』结尾」逐级判定，顺序：

    ① address 组件里第一个以「市」结尾的（state/city/municipality/town/county/…）；
    ② 否则在 display_name 里从右往左找以「市」结尾的段——它才是地级市/直辖市名（长串里的区、
       省、邮编都不以「市」结尾；区以「区」结尾，所以不会被误取）；
    ③ 否则退回更粗一级的可用组件（city → municipality → town → village → county →
       state_district → state → province → suburb），例如只有「示例区 + 示例省」时如实返回示例区；
    ④ 都取不到 → 返回空，由调用方明确失败；绝不编造地名。

    为什么不直接用 display_name：它是「街道, 区, 市, 省, 邮编, 国家」长串，既非城市粒度，
    也无法与用户手填的城市名（或「沿用上期」的历史值）对得上。
    """
    def _norm(value: Any) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):  # 高德等 provider 偶尔把该字段返回成数组
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return ""

    ordered = ("state", "city", "municipality", "town", "county", "village",
               "state_district", "province", "suburb")
    components = [(k, _norm(addr.get(k))) for k in ordered]
    components = [(k, v) for k, v in components if v]

    # ① address 里明确以「市」结尾的组件（普通地级市常见：city=示例市）
    for _, v in components:
        if v.endswith("市"):
            return v
    # ② display_name 里的「市」段：直辖市与「address 只给区」的情形都靠它落到市名
    parts = [p.strip() for p in str(display_name or "").split(",") if p.strip()]
    for part in reversed(parts):
        if part.endswith("市"):
            return part
    # ③ 组件粒度回退（与「城市优先」一致，不按 address 的键序）
    for key in ("city", "municipality", "town", "village", "county", "state_district", "state", "province", "suburb"):
        for k, v in components:
            if k == key:
                return v
    # ④ display_name 首段兜底（非中文地址等）
    return parts[0] if parts else ""


def nominatim_fetch(lat: float, lon: float) -> tuple[str, str]:
    """调用 Nominatim 逆地理编码，返回 (text, raw)。text 为城市级名称。"""
    _throttle()
    params = urllib.parse.urlencode({
        "format": "json",
        "lat": f"{lat}",
        "lon": f"{lon}",
        "zoom": "10",
        "addressdetails": "1",  # 需要 address 组件才能取城市级名称
        "accept-language": "zh",
    })
    url = f"https://nominatim.openstreetmap.org/reverse?{params}"
    headers = {"User-Agent": "networth/1.0 (+personal ledger)"}
    try:
        raw = _http_get(url, headers=headers)
        data = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise GeocodeUnavailable(f"nominatim 请求失败：{exc}") from exc
    # 解析：城市级优先（address 组件 → display_name 里以「市」结尾的段 → display_name 首段）
    address = data.get("address") or {}
    text = _strip_city_suffix(_city_level(address, str(data.get("display_name") or "")))
    if not text:
        raise GeocodeUnavailable("nominatim 返回为空")
    return text, raw


def amap_fetch(lat: float, lon: float) -> tuple[str, str]:
    """调用高德逆地理编码，返回 (text, raw)。"""
    key = os.environ.get("NETWORTH_AMAP_KEY", "").strip()
    if not key:
        raise GeocodeUnavailable("amap 未配置 NETWORTH_AMAP_KEY")
    # 高德要求 location=lon,lat
    params = urllib.parse.urlencode({
        "key": key,
        "location": f"{lon},{lat}",
    })
    url = f"https://restapi.amap.com/v3/geocode/regeo?{params}"
    try:
        raw = _http_get(url, timeout=8)
        data = json.loads(raw)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise GeocodeUnavailable(f"amap 请求失败：{exc}") from exc
    if str(data.get("status")) != "1":
        raise GeocodeUnavailable(f"amap 返回错误：{data.get('info') or data}")
    regeo = data.get("regeocode") or {}
    comp = regeo.get("addressComponent") or {}
    # 与 nominatim 同口径：地点字段取城市级（直辖市时 city 可能为空数组 → 回退 province）
    text = _strip_city_suffix(_city_level(comp))
    if not text:
        # 回退：formatted_address 的首段（去掉区/街道长串）
        formatted = str(regeo.get("formatted_address") or "").strip()
        text = formatted.split("区")[0].strip() if formatted else ""
    if not text.strip():
        raise GeocodeUnavailable("amap 返回为空")
    return text.strip(), raw


def resolve(
    store: Store,
    lat: float,
    lon: float,
    *,
    provider: str | None = None,
    refresh: bool = False,
    fetcher: Fetcher | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """逆地理编码主入口：先查缓存，必要时抓取并写入缓存。

    provider 由环境变量 NETWORTH_GEOCODE_PROVIDER 决定（off|nominatim|amap），
    传入 provider 参数时优先使用（便于测试注入）。
    fetcher 可注入（测试用假抓取器，避免打真实网络）。
    """
    # 参数校验已在 domain 层完成，这里再做一次防御性检查
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise GeocodeUnavailable("坐标超出范围")

    prov = (provider or os.environ.get("NETWORTH_GEOCODE_PROVIDER") or "off").strip().lower()
    if prov not in {"off", "nominatim", "amap"}:
        prov = "off"
    if prov == "off":
        raise GeocodeUnavailable("provider_disabled")
    if prov == "amap" and not fetcher:
        # 提前检查 key，避免粗化后仍去查缓存再报错（保持与 spec 一致：应返回 provider_not_configured）
        if not os.environ.get("NETWORTH_AMAP_KEY", "").strip():
            raise GeocodeUnavailable("provider_not_configured")

    lat_key, lon_key = quantize(lat, lon)

    if not refresh:
        row = store.get_geocode(prov, lat_key, lon_key)
        if row is not None:
            return {"text": row["text"], "provider": prov, "cached": True, "lat_key": lat_key, "lon_key": lon_key}

    # 需要抓取
    fetched_at = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if fetcher is not None:
        try:
            text, raw = fetcher(lat, lon)
        except GeocodeUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GeocodeUnavailable(f"抓取失败：{exc}") from exc
    else:
        if prov == "nominatim":
            text, raw = nominatim_fetch(lat, lon)
        elif prov == "amap":
            text, raw = amap_fetch(lat, lon)
        else:
            raise GeocodeUnavailable("provider_disabled")

    if not text or not text.strip():
        raise GeocodeUnavailable("抓取结果为空")
    # 写入缓存（粗化后的键）
    store.upsert_geocode(prov, lat_key, lon_key, text.strip(), fetched_at, raw)
    return {"text": text.strip(), "provider": prov, "cached": False, "lat_key": lat_key, "lon_key": lon_key}
