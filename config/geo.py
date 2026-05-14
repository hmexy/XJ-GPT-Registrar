# -*- coding: utf-8 -*-
"""
代理出口地理位置探测

注册指纹里的时区/语言/Accept-Language 必须和实际出口 IP 的地理位置一致，
否则会触发 OpenAI 风控（最常见症状：注册成功但很快被封）。

本模块通过当前代理调用 ipinfo.io 拿到出口 IP 的国家/时区，再映射成
完整的指纹 profile（language / languages / tz_string / tz_label）。

外部 API 不通时会回退到 en-US/UTC 默认值，并打 WARNING 提示用户。
"""
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)


# 国家码 → (navigator.language, navigator.languages CSV, Chrome Date.toString 时区括号注释)
# 括号注释抓的是 Chrome 在对应系统语言下的实际输出，确保 Date.toString() 看起来像
# 一个真实跑在该 locale 系统上的浏览器。
_COUNTRY_PROFILE: dict[str, tuple[str, str, str]] = {
    "JP": ("ja-JP", "ja-JP,ja,en-US,en", "日本標準時"),
    "US": ("en-US", "en-US,en", "Pacific Daylight Time"),
    "CN": ("zh-CN", "zh-CN,zh,en-US,en", "中国标准时间"),
    "HK": ("zh-HK", "zh-HK,zh,en-US,en", "香港標準時間"),
    "TW": ("zh-TW", "zh-TW,zh,en-US,en", "台北標準時間"),
    "SG": ("en-SG", "en-SG,en,zh-SG,zh", "Singapore Standard Time"),
    "KR": ("ko-KR", "ko-KR,ko,en-US,en", "대한민국 표준시"),
    "DE": ("de-DE", "de-DE,de,en-US,en", "Mitteleuropäische Sommerzeit"),
    "GB": ("en-GB", "en-GB,en", "British Summer Time"),
    "FR": ("fr-FR", "fr-FR,fr,en-US,en", "heure d’été d’Europe centrale"),
    "NL": ("nl-NL", "nl-NL,nl,en-US,en", "Midden-Europese Zomertijd"),
    "CA": ("en-CA", "en-CA,en,fr-CA,fr", "Eastern Daylight Time"),
    "AU": ("en-AU", "en-AU,en", "Australian Eastern Daylight Time"),
    "IN": ("en-IN", "en-IN,en,hi-IN,hi", "India Standard Time"),
    "RU": ("ru-RU", "ru-RU,ru,en-US,en", "Москва, стандартное время"),
    "BR": ("pt-BR", "pt-BR,pt,en-US,en", "Horário Padrão de Brasília"),
    "ID": ("id-ID", "id-ID,id,en-US,en", "Waktu Indonesia Barat"),
    "VN": ("vi-VN", "vi-VN,vi,en-US,en", "Giờ Đông Dương"),
    "TH": ("th-TH", "th-TH,th,en-US,en", "เวลามาตรฐานในไทย"),
    "MY": ("en-MY", "en-MY,en,ms-MY,ms", "Waktu Piawai Malaysia"),
    "PH": ("en-PH", "en-PH,en,fil-PH,fil", "Philippine Standard Time"),
}

_DEFAULT_PROFILE = ("en-US", "en-US,en,en-GB", "Coordinated Universal Time")


_cache_lock = threading.Lock()
_cache: dict[str, dict] = {}


def _format_offset(minutes: int) -> str:
    """把分钟数格式化成 Chrome 的 GMT+HHMM / GMT-HHMM 形式。"""
    sign = "+" if minutes >= 0 else "-"
    abs_min = abs(minutes)
    return f"GMT{sign}{abs_min // 60:02d}{abs_min % 60:02d}"


def _build_profile(country: str, tz_name: str | None) -> dict:
    """根据国家码与 IANA 时区名构造指纹 profile。"""
    lang, languages, tz_label = _COUNTRY_PROFILE.get(country.upper(), _DEFAULT_PROFILE)

    offset_min = 0
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
            if offset is not None:
                offset_min = int(offset.total_seconds() / 60)
        except Exception as exc:  # ZoneInfo 不识别 / Windows 没装 tzdata
            logger.debug(f"[Geo] zoneinfo 解析 {tz_name} 失败: {exc}")

    return {
        "country": country.upper() or "??",
        "tz_name": tz_name or "",
        "language": lang,
        "languages": languages,           # CSV
        "tz_offset_minutes": offset_min,
        "tz_string": _format_offset(offset_min),
        "tz_label": tz_label,
    }


def build_default_profile() -> dict:
    """无代理探测可用时的兜底 profile。"""
    return _build_profile("", None)


def accept_language_header(geo: dict) -> str:
    """从 geo profile 生成 Accept-Language 头（带 q 权重，匹配 Chrome 行为）。"""
    items = [item.strip() for item in geo["languages"].split(",") if item.strip()]
    if not items:
        return "en-US,en;q=0.9"
    parts = [items[0]]
    q = 9
    for lang in items[1:]:
        parts.append(f"{lang};q=0.{q}")
        q = max(1, q - 1)
    return ",".join(parts)


def detect_geo(proxy: str | None, timeout: float = 5.0) -> dict:
    """
    通过指定代理探测出口 IP 的国家/时区，返回完整的指纹 profile。

    关键设计：先打 `chatgpt.com/cdn-cgi/trace` 拿到 **OpenAI 实际看到的出口 IP**，
    再用那个 IP 反查时区。这样即便代理做了 OpenAI 分流（chatgpt.com 走 A 节点、
    其他流量走 B 节点），也能拿到正确的"对 OpenAI 表现出的"地理位置。

    Args:
        proxy: 代理 URL；"" 或 None 表示直连
        timeout: HTTP 探测超时（秒）

    Returns:
        指纹 profile dict（同 _build_profile 返回值）；探测失败时回退默认 profile。
        从不抛异常 —— 失败只打 WARNING，不阻塞注册主流程。
    """
    cache_key = proxy or "direct"
    with _cache_lock:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached

    profile = build_default_profile()
    try:
        from curl_cffi.requests import Session

        s = Session(impersonate="chrome142")
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        s.timeout = timeout

        # ---- Step 1: 通过 chatgpt.com 拿 OpenAI 实际看到的出口 IP + CF 国家码 ----
        ip: str | None = None
        country_cf: str | None = None
        try:
            resp = s.get("https://chatgpt.com/cdn-cgi/trace")
            resp.raise_for_status()
            trace: dict[str, str] = {}
            for line in (resp.text or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    trace[k.strip()] = v.strip()
            ip = trace.get("ip") or None
            country_cf = (trace.get("loc") or "").upper() or None
            logger.debug(
                f"[Geo] chatgpt.com/cdn-cgi/trace: ip={ip} loc={country_cf} "
                f"colo={trace.get('colo')}"
            )
        except Exception as exc:
            logger.warning(
                f"[Geo] chatgpt.com trace 失败 ({type(exc).__name__}: {exc})，"
                f"将回退到直查 ipinfo（如果代理做了 OpenAI 分流，结果可能不准）"
            )

        # ---- Step 2: 用 Step 1 拿到的 IP 反查精确时区（ipinfo 按 IP 查询，与 client 路由无关）----
        country = country_cf or ""
        tz_name: str | None = None
        try:
            if ip:
                # 用 IP 反查：返回的是该 IP 对应的国家/时区，与本机走哪个出口无关
                resp = s.get(f"https://ipinfo.io/{ip}/json")
                resp.raise_for_status()
                data = resp.json()
                if not country:
                    country = (data.get("country") or "").upper()
                tz_name = data.get("timezone") or None
            else:
                # 没拿到 chatgpt.com 的 IP（trace 端点不通），只能直查 client IP —— 不准
                resp = s.get("https://ipinfo.io/json")
                resp.raise_for_status()
                data = resp.json()
                country = (data.get("country") or "").upper()
                tz_name = data.get("timezone") or None
                ip = data.get("ip") or "?"
        except Exception as exc:
            logger.warning(
                f"[Geo] ipinfo 查询失败 ({type(exc).__name__}: {exc})；"
                f"将仅依赖 CF loc 字段，时区可能不准"
            )

        if country:
            profile = _build_profile(country, tz_name)
            via = "chatgpt.com" if country_cf else "ipinfo (无分流回退)"
            logger.info(
                f"[Geo] 代理出口 ip={ip or '?'} country={country} tz={tz_name or '(未知)'} "
                f"(来源: {via}) → 指纹 language={profile['language']} "
                f"tz={profile['tz_string']} ({profile['tz_label']})"
            )
        else:
            logger.warning(
                f"[Geo] 无法确定出口国家，回退默认 profile={profile['language']}/{profile['tz_string']}"
            )
    except Exception as exc:
        logger.warning(
            f"[Geo] 代理出口探测失败 ({type(exc).__name__}: {exc})，"
            f"回退到默认 profile={profile['language']}/{profile['tz_string']}；"
            f"若长期失败，请检查代理可用性或在 config/geo.py 手动指定 profile"
        )

    with _cache_lock:
        _cache[cache_key] = profile
    return profile


def reset_cache() -> None:
    """清空缓存（用于 Web GUI 切换代理后强制重新探测）。"""
    with _cache_lock:
        _cache.clear()
