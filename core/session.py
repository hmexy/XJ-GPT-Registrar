# -*- coding: utf-8 -*-
"""
curl_cffi Session 封装
统一管理 Cookie、请求头和 TLS 指纹
"""
import hashlib
import random as _random_module
import uuid

from curl_cffi.requests import Session

from config import (
    USER_AGENT, SEC_CH_UA, SEC_CH_UA_PLATFORM, SEC_CH_UA_MOBILE,
    IMPERSONATE, REQUEST_TIMEOUT, pick_proxy,
    detect_geo, accept_language_header,
)


# 常见 Chrome 用户的屏幕与 CPU 档位（避免每个号都长得像 1920x1080 / 32 核工作站）
_SCREEN_PROFILES: list[tuple[int, int]] = [
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1680, 1050),
    (1920, 1080),
    (2560, 1440),
]
_HARDWARE_CONCURRENCY_OPTIONS: list[int] = [4, 6, 8, 8, 12, 12, 16]


def _device_rng(device_id: str) -> _random_module.Random:
    """从 device_id 派生确定性 RNG：同一个注册流程多次取指纹时保持一致。"""
    seed = int(hashlib.md5(device_id.encode("utf-8")).hexdigest()[:8], 16)
    return _random_module.Random(seed)


class BrowserSession:
    """
    模拟 Chrome 浏览器的 HTTP 会话管理器。
    使用 curl_cffi 的 impersonate 功能绕过 Cloudflare TLS 指纹检测。
    """

    def __init__(self, proxy: str = None):
        """
        初始化会话。

        Args:
            proxy: 代理地址，如 "socks5h://user:pass@host:port"。
                   不传则从 config.PROXY_POOL 随机抽一个。
                   显式传 "" 表示禁用代理。
        """
        # proxy=None  → 从池里随机抽（默认行为）
        # proxy=""    → 禁用代理（直连）
        # proxy="..." → 使用指定代理
        if proxy is None:
            self.proxy = pick_proxy()
        else:
            self.proxy = proxy

        # 生成设备ID（oai-did），整个注册流程复用
        self.device_id = str(uuid.uuid4())

        # 生成 auth_session_logging_id
        self.auth_session_logging_id = str(uuid.uuid4())

        # 探测出口 IP 地理位置 → 决定指纹时区/语言。
        # 失败时回退到默认 profile（不抛异常，但会在日志里 WARNING）
        self.geo: dict = detect_geo(self.proxy)

        # 基于 device_id 生成确定性的"机器画像"，整个注册流程保持一致
        rng = _device_rng(self.device_id)
        self.screen_width, self.screen_height = rng.choice(_SCREEN_PROFILES)
        self.hardware_concurrency = rng.choice(_HARDWARE_CONCURRENCY_OPTIONS)

        # 创建 curl_cffi 会话
        self.session = Session(impersonate=IMPERSONATE)

        # 设置代理
        if self.proxy:
            self.session.proxies = {
                "http": self.proxy,
                "https": self.proxy,
            }

        # 设置超时
        self.session.timeout = REQUEST_TIMEOUT

    def _get_common_headers(self) -> dict:
        """获取通用请求头"""
        return {
            "User-Agent": USER_AGENT,
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-platform": SEC_CH_UA_PLATFORM,
            "sec-ch-ua-mobile": SEC_CH_UA_MOBILE,
            "accept-language": accept_language_header(self.geo),
        }

    def get_chatgpt_headers(self, referer: str = "https://chatgpt.com/login") -> dict:
        """
        获取 chatgpt.com 域名的请求头。
        用于步骤1-3。
        """
        headers = self._get_common_headers()
        headers.update({
            "accept": "*/*",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "priority": "u=1, i",
        })
        return headers

    def get_auth_headers(self, referer: str = "https://auth.openai.com/create-account/password") -> dict:
        """
        获取 auth.openai.com 域名的请求头。
        用于步骤7、10、12。
        """
        headers = self._get_common_headers()
        headers.update({
            "accept": "application/json",
            "content-type": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": referer,
            "priority": "u=1, i",
            "origin": "https://auth.openai.com",
        })
        return headers

    def get_auth_navigate_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        """
        获取 auth.openai.com 导航请求头（用于GET页面请求）。
        用于步骤4、5、8。
        """
        headers = self._get_common_headers()
        headers.update({
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "navigate",
            "sec-fetch-dest": "document",
            "referer": referer,
            "priority": "u=0, i",
            "upgrade-insecure-requests": "1",
        })
        return headers

    def get_sentinel_headers(self) -> dict:
        """
        获取 sentinel.openai.com 的请求头。
        用于步骤6、9、11。
        """
        from config import SENTINEL_SV
        headers = self._get_common_headers()
        headers.update({
            "accept": "*/*",
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://sentinel.openai.com",
            "referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_SV}",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "priority": "u=1, i",
        })
        return headers

    def get(self, url: str, headers: dict = None, **kwargs):
        """发送 GET 请求"""
        return self.session.get(url, headers=headers, **kwargs)

    def post(self, url: str, headers: dict = None, **kwargs):
        """发送 POST 请求"""
        return self.session.post(url, headers=headers, **kwargs)
