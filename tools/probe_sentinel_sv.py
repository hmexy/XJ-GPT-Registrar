# -*- coding: utf-8 -*-
"""
探测 OpenAI 当前的 Sentinel SDK 版本号（SENTINEL_SV）。

权威方法：直接请求 sentinel.openai.com/backend-api/sentinel/frame.html（不带 ?sv= 参数），
服务器会把当前线上的默认 sv 写到响应 HTML 的 `<script src=".../sentinel/<SV>/sdk.js">` 里。

用法（项目根目录）：
    python tools/probe_sentinel_sv.py                  # 用 config.PROXY_POOL
    python tools/probe_sentinel_sv.py http://127.0.0.1:7890
    python tools/probe_sentinel_sv.py ""               # 直连
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curl_cffi.requests import Session

FRAME_URL = "https://sentinel.openai.com/backend-api/sentinel/frame.html"
PAT_SDK = re.compile(r"sentinel/([0-9a-f]{10,16})/sdk\.js", re.IGNORECASE)


def _pick_proxy(arg: str | None) -> str | None:
    if arg is None:
        try:
            from config import pick_proxy
            return pick_proxy() or None
        except Exception:
            return None
    return arg or None


def probe(proxy: str | None) -> None:
    s = Session(impersonate="chrome142")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    s.timeout = 20
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }

    print(f"代理: {proxy or '(直连)'}", file=sys.stderr)
    print(f"请求 {FRAME_URL} ...", file=sys.stderr)

    try:
        r = s.get(FRAME_URL, headers=headers)
    except Exception as exc:
        print(f"请求失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"HTTP {r.status_code}, body len={len(r.text)}", file=sys.stderr)
    matches = list({m.group(1) for m in PAT_SDK.finditer(r.text or "")})
    if not matches:
        print("未在响应中匹配到 sentinel/<sv>/sdk.js", file=sys.stderr)
        print(f"响应前 500 字: {(r.text or '')[:500]}", file=sys.stderr)
        sys.exit(2)

    if len(matches) > 1:
        print(f"匹配到多个版本: {matches}", file=sys.stderr)

    current = matches[0]

    # 读取本地配置做对比
    try:
        from config import SENTINEL_SV as LOCAL_SV
    except Exception:
        LOCAL_SV = "??"

    print()
    print(f"线上当前 SENTINEL_SV: {current}")
    print(f"本地 SENTINEL_SV:    {LOCAL_SV}")
    if current == LOCAL_SV:
        print("→ 本地与线上一致，无需更新。")
    else:
        print(f"→ 不一致！请修改 config/openai_protocol.py:")
        print(f"     SENTINEL_SV = \"{current}\"")
        print(f"   然后下载新版 sdk.js:")
        print(f'     curl -o sentinel/sdk.js "https://sentinel.openai.com/sentinel/{current}/sdk.js"')


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    probe(_pick_proxy(arg))
