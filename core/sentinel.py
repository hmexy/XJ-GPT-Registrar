# -*- coding: utf-8 -*-
"""
Sentinel Token 生成模块
逆向自 sentinel.openai.com 的 sdk.js

核心逻辑：
1. 生成 `p` 字段（浏览器指纹数据，base64编码的 JSON 数组）
2. 发送 POST 请求到 sentinel.openai.com/backend-api/sentinel/req
3. 解析响应中的 token、turnstile、proofofwork
4. 计算 Proof of Work（FNV-1a 哈希）
5. 组装最终的 openai-sentinel-token 请求头值
"""
import json
import time
import random
import base64
from datetime import datetime
from typing import TYPE_CHECKING, Any

from config import USER_AGENT, SENTINEL_SV

if TYPE_CHECKING:
    from core.session import BrowserSession


# config[10] 候选：常见 navigator 原型方法的 toString 表征
_NAVIGATOR_PROPS = [
    "clearOriginJoinedAdInterestGroups−function clearOriginJoinedAdInterestGroups() { [native code] }",
    "canLoadAdAuctionFencedFrame−function canLoadAdAuctionFencedFrame() { [native code] }",
    "clipboard−[object Clipboard]",
    "getBattery−function getBattery() { [native code] }",
    "getGamepads−function getGamepads() { [native code] }",
    "javaEnabled−function javaEnabled() { [native code] }",
    "sendBeacon−function sendBeacon() { [native code] }",
    "vibrate−function vibrate() { [native code] }",
]


def _date_toString(geo: dict | None) -> str:
    """
    模拟 Chrome 的 Date.prototype.toString() 输出。
    格式：'Sun Apr 19 2026 19:46:20 GMT+0900 (日本標準時)'

    时区与括号注释必须与代理出口 IP 的 geo 一致，否则与 IP 错配 → 被风控。
    """
    if geo is None:
        from config import build_default_profile
        geo = build_default_profile()

    offset_min = geo["tz_offset_minutes"]
    # 当前 UTC 时间 + 出口时区偏移 = 出口"墙上时间"
    now = datetime.utcfromtimestamp(time.time() + offset_min * 60)
    return now.strftime(
        f"%a %b %d %Y %H:%M:%S {geo['tz_string']} ({geo['tz_label']})"
    )


def generate_fingerprint_data(
    device_id: str,
    *,
    geo: dict | None = None,
    screen_width: int = 1920,
    screen_height: int = 1080,
    hardware_concurrency: int = 8,
    attempt: int | None = None,
    elapsed_ms: float | None = None,
    rng: random.Random | None = None,
) -> list:
    """
    生成浏览器指纹数据（p 字段）。
    对应 SDK 中 getConfig() + N() 函数。

    Args:
        device_id: 设备ID（写入 config[14] sid）
        geo: 代理出口地理 profile（决定时区/语言/Date 字符串）
        screen_width / screen_height: 屏幕尺寸（决定 config[0]）
        hardware_concurrency: navigator.hardwareConcurrency
        attempt: PoW 尝试次数；为 None 表示 requirements_token 模式，
                 此时 config[3] 填 Math.random()
        elapsed_ms: PoW 累计耗时；为 None 表示 requirements_token 模式，
                    此时 config[9] 填 Math.random()
        rng: 可选 RNG（PoW 内多次调用时复用以减少开销）
    """
    rng = rng or random
    if geo is None:
        from config import build_default_profile
        geo = build_default_profile()

    date_str = _date_toString(geo)

    # 关键修复：performance.timeOrigin + performance.now() ≈ Date.now()
    # 真浏览器里这两个值总是自洽的，不能分别用独立随机数填。
    now_ms = time.time() * 1000
    perf_now = round(rng.uniform(1000, 50000), 10)            # 页面加载后已经过去的毫秒数
    time_origin = round(now_ms - perf_now, 1)                  # 页面"开始加载"的绝对时间戳

    # config[3] / config[9] 的填充规则：
    #   - requirements_token 模式（attempt=None）: 都填 Math.random() 浮点
    #   - PoW 模式（attempt 为整数）: [3]=attempt（nonce）、[9]=耗时毫秒（整数）
    if attempt is None:
        c3: Any = rng.random()
    else:
        c3 = int(attempt)
    if elapsed_ms is None:
        c9: Any = rng.random()
    else:
        c9 = int(round(elapsed_ms))

    config = [
        int(screen_width) + int(screen_height),                # [0] screen.width + screen.height
        date_str,                                              # [1] Date.toString()
        4294967296,                                            # [2] jsHeapSizeLimit (Chrome 4GB)
        c3,                                                    # [3] Math.random() / PoW nonce
        USER_AGENT,                                            # [4] UA
        f"https://sentinel.openai.com/sentinel/{SENTINEL_SV}/sdk.js",  # [5] currentScript.src
        None,                                                  # [6] documentElement[data-build] (auth.openai.com 上为 null)
        geo["language"],                                       # [7] navigator.language
        geo["languages"],                                      # [8] navigator.languages.join(",")
        c9,                                                    # [9] Math.random() / PoW 耗时
        rng.choice(_NAVIGATOR_PROPS),                          # [10] 随机 navigator 原型方法
        "_reactListening" + "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=11)),  # [11] document 随机 key
        rng.choice(["requestIdleCallback", "webkitRequestAnimationFrame", "onfocus", "onblur"]),  # [12] window 随机 key
        perf_now,                                              # [13] performance.now()
        str(device_id),                                        # [14] sid
        "",                                                    # [15] location.search
        int(hardware_concurrency),                             # [16] hardwareConcurrency
        time_origin,                                           # [17] performance.timeOrigin
        0,                                                     # [18] "ai" in window
        0,                                                     # [19] "InstallTrigger" in window (Firefox)
        0,                                                     # [20] "cache" in window
        0,                                                     # [21] "data" in window
        0,                                                     # [22] "solana" in window
        0,                                                     # [23] "dump" in window (Firefox)
        0,                                                     # [24] "requestIdleCallback" in window
    ]
    return config


def encode_config(config: list) -> str:
    """
    将 config 数组编码为 base64 字符串。
    对应 SDK 中的 N() 函数：
        JSON.stringify(t) → TextEncoder.encode() → btoa(String.fromCharCode(...))

    注意：SDK 使用 TextEncoder 将 JSON字符串 编码为 UTF-8 字节，然后逐个字节 btoa。
    这等效于 Python 的：json_str.encode('utf-8') → base64 encode
    """
    json_str = json.dumps(config, ensure_ascii=False, separators=(',', ':'))
    encoded = base64.b64encode(json_str.encode('utf-8')).decode('ascii')
    return encoded


def fnv1a_hash(text: str) -> str:
    """
    FNV-1a 哈希算法（32位）。
    对应 SDK 中的哈希函数，用于 Proof of Work 校验。
    """
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = _imul(h, 16777619) & 0xFFFFFFFF

    h ^= (h >> 16)
    h = _imul(h, 2246822507) & 0xFFFFFFFF
    h ^= (h >> 13)
    h = _imul(h, 3266489909) & 0xFFFFFFFF
    h ^= (h >> 16)
    h = h & 0xFFFFFFFF

    return format(h, '08x')


def _imul(a: int, b: int) -> int:
    """模拟 JavaScript 的 Math.imul（32位整数乘法）。"""
    a = a & 0xFFFFFFFF
    b = b & 0xFFFFFFFF
    return (a * b) & 0xFFFFFFFF


def _session_fp_kwargs(session: "BrowserSession") -> dict:
    """从 BrowserSession 抽取指纹相关参数（geo / 屏幕 / 核心数）。"""
    return {
        "geo": session.geo,
        "screen_width": session.screen_width,
        "screen_height": session.screen_height,
        "hardware_concurrency": session.hardware_concurrency,
    }


def solve_proof_of_work(
    seed: str,
    difficulty: str,
    session: "BrowserSession",
    max_attempts: int = 500000,
) -> str:
    """
    计算 Proof of Work。

    SDK 中 _runCheck 的逻辑：
    1. 将 config[3] 设为尝试次数（nonce）
    2. 将 config[9] 设为 Math.round(performance.now() - startTime)
    3. 编码 config → base64 字符串 c
    4. 计算 fnv1a(seed + c) → 8位hex
    5. 如果 hex[:len(difficulty)] <= difficulty，则返回 c + "~S"

    Args:
        seed: 服务端返回的 seed
        difficulty: 服务端返回的 difficulty（16进制前缀）
        session: 浏览器会话（提供 device_id 与指纹画像）
        max_attempts: 最大尝试次数

    Returns:
        PoW 答案字符串，格式为 base64_encoded_config + "~S"
    """
    start_time = time.time() * 1000  # 毫秒
    fp_kwargs = _session_fp_kwargs(session)
    rng = random.Random()

    # 先生成一份基础 config，PoW 循环里只改 [3]/[9]/[13]
    config = generate_fingerprint_data(
        session.device_id, attempt=0, elapsed_ms=0, rng=rng, **fp_kwargs,
    )

    diff_len = len(difficulty)

    for i in range(max_attempts):
        elapsed = time.time() * 1000 - start_time
        config[3] = i
        config[9] = int(round(elapsed))
        # performance.now() 同步推进，保持自洽（与 time_origin 之差始终 ≈ Date.now()）
        config[13] = round(config[17] - start_time + elapsed, 10)

        encoded = encode_config(config)
        hash_input = seed + encoded
        hash_result = fnv1a_hash(hash_input)

        if hash_result[:diff_len] <= difficulty:
            return encoded + "~S"

    # 达到最大尝试次数仍未找到 → 返回错误前缀（与原行为保持一致）
    return "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + encode_config(["e"])


def generate_requirements_token(session: "BrowserSession") -> str:
    """
    生成 requirements token（首次 sentinel/req 的 p 字段值）。
    对应 SDK 中的 getRequirementsToken() / _generateRequirementsTokenAnswerBlocking()

    这是第一次调用 sentinel/req 时 p 字段的值。
    它就是简单的 config 编码 + "~S" 后缀，不需要 PoW。
    """
    config = generate_fingerprint_data(
        session.device_id, **_session_fp_kwargs(session),
    )
    encoded = encode_config(config)
    return "gAAAAAC" + encoded + "~S"


def build_sentinel_request_body(p: str, device_id: str, flow: str) -> str:
    """
    构建 sentinel/req 的请求体。
    """
    body = {"p": p, "id": device_id, "flow": flow}
    return json.dumps(body, separators=(',', ':'))


def build_sentinel_token_header(
    p: str,
    turnstile_token: str,
    sentinel_token: str,
    device_id: str,
    flow: str,
) -> str:
    """
    构建 openai-sentinel-token 请求头的值。
    """
    header_value = {
        "p": p,
        "t": turnstile_token or "",
        "c": sentinel_token,
        "id": device_id,
        "flow": flow,
    }
    return json.dumps(header_value, separators=(',', ':'))


def get_enforcement_token(
    sentinel_response: dict,
    seed: str,
    difficulty: str,
    session: "BrowserSession",
) -> str:
    """
    在有 PoW 要求时，计算 enforcement token（带 PoW 的 p 字段）。
    """
    pow_data = sentinel_response.get("proofofwork", {})

    if pow_data.get("required"):
        pow_seed = pow_data.get("seed", "")
        pow_difficulty = pow_data.get("difficulty", "")
        answer = solve_proof_of_work(pow_seed, pow_difficulty, session)
        return "gAAAAAB" + answer

    return generate_requirements_token(session)
