# -*- coding: utf-8 -*-
"""
Sentinel Runner 适配层
通过 subprocess 调用项目根目录的 sentinel-runner.js，
让 Node.js 在 vm 沙箱中真实运行 sdk.js，生成可通过校验的 sentinel-token。

工作原理：
1. Python 端先调用 sentinel.openai.com/backend-api/sentinel/req 拿到 challenge JSON
2. 把 challenge 写入临时文件
3. 调用 node sentinel-runner.js --challenge-file <临时文件> ...
4. 捕获 stdout 即为 openai-sentinel-token 的 value
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from config import USER_AGENT, SENTINEL_SV

if TYPE_CHECKING:
    from core.session import BrowserSession

logger = logging.getLogger(__name__)

# 项目根目录（core 的上一级）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Node 资源放在项目根的 sentinel/ 子目录下
_SENTINEL_DIR = _PROJECT_ROOT / "sentinel"
_RUNNER_PATH = _SENTINEL_DIR / "sentinel-runner.js"
_SDK_PATH = _SENTINEL_DIR / "sdk.js"

# 各 flow 对应的 page-url（与浏览器实际页面一致，影响 sdk.js 指纹生成）
_FLOW_PAGE_URL = {
    "username_password_create": "https://auth.openai.com/create-account/password",
    "authorize_continue": "https://auth.openai.com/email-verification",
    "oauth_create_account": "https://auth.openai.com/about-you",
}

# Node 子进程超时（秒）。sdk.js 内部可能要做 PoW，留充裕一点
_RUNNER_TIMEOUT = 60


def _resolve_node_executable() -> str:
    """
    解析 Node 可执行文件名。Windows 下默认 node.exe，类 Unix 为 node。
    允许通过环境变量 NODE_EXECUTABLE 覆盖。
    """
    override = os.environ.get("NODE_EXECUTABLE")
    if override:
        return override
    return "node.exe" if sys.platform.startswith("win") else "node"


def _ensure_runner_environment() -> None:
    """启动前的强制检查：runner.js / sdk.js 必须存在。"""
    if not _RUNNER_PATH.exists():
        raise FileNotFoundError(f"找不到 sentinel-runner.js: {_RUNNER_PATH}")
    if not _SDK_PATH.exists():
        raise FileNotFoundError(f"找不到 sdk.js: {_SDK_PATH}")


def generate_sentinel_token(
    challenge: dict,
    flow: str,
    session: "BrowserSession",
    page_url: str | None = None,
) -> str:
    """
    把 sentinel.openai.com 返回的 challenge 喂给 sdk.js，生成最终 sentinel-token 字符串。

    Args:
        challenge: sentinel/req 返回的完整 JSON
        flow: 流程标识
        session: 浏览器会话（提供 device_id / UA / 屏幕 / 核心数 / 出口 geo profile）
        page_url: 当前页面 URL（影响 referer / location 指纹）；默认按 flow 推断
    """
    _ensure_runner_environment()

    if not flow:
        raise ValueError("flow 不能为空")
    if not session.device_id:
        raise ValueError("session.device_id 不能为空")

    page = page_url or _FLOW_PAGE_URL.get(
        flow, "https://auth.openai.com/create-account/password"
    )
    geo = session.geo

    # 把 challenge 写入临时文件，避免命令行长度 / 转义问题
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix=f"sentinel-challenge-{flow}-",
        delete=False,
        encoding="utf-8",
    )
    try:
        json.dump(challenge, tmp, ensure_ascii=False)
        tmp.flush()
        tmp.close()

        cmd = [
            _resolve_node_executable(),
            str(_RUNNER_PATH),
            "--challenge-file", tmp.name,
            "--flow", flow,
            "--device-id", session.device_id,
            "--page-url", page,
            "--user-agent", USER_AGENT,
            "--sdk", str(_SDK_PATH),
            # 关键修复：scriptSrc 必须指向真实的 sentinel.openai.com sdk.js，
            # 否则 sandbox 里 currentScript.src 会跟 Python 端的 sentinel referer
            # 自相矛盾（一边是 chatgpt.com 一边是 sentinel.openai.com）。
            "--script-src", f"https://sentinel.openai.com/sentinel/{SENTINEL_SV}/sdk.js",
            # 关键修复：auth.openai.com 的页面没有 data-build 属性。
            # --no-build-id 让 sandbox 不挂这个属性，getAttribute 自然返回 null。
            "--no-build-id",
            # 指纹画像与 Python 端 BrowserSession 完全一致（每个号都从 device_id 派生）
            "--width", str(session.screen_width),
            "--height", str(session.screen_height),
            "--cores", str(session.hardware_concurrency),
            # 语言/区域随代理出口动态切换
            "--language", geo["language"],
            "--languages", geo["languages"],
            "--no-cookie",
        ]

        logger.info(
            f"[SentinelRunner] 调用 Node 生成 token, flow={flow}, "
            f"geo={geo['country']}/{geo['language']}, "
            f"screen={session.screen_width}x{session.screen_height}, "
            f"cores={session.hardware_concurrency}"
        )
        logger.debug(f"[SentinelRunner] 命令: {' '.join(cmd)}")

        # 关键：禁用 sentinel.config.json 自动发现（避免外部配置干扰）
        env = os.environ.copy()
        env.pop("SENTINEL_CONFIG", None)
        env["SENTINEL_CONFIG"] = "__none__"  # 故意指向不存在的文件，跳过 fallback 列表

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(_PROJECT_ROOT),
                timeout=_RUNNER_TIMEOUT,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"sentinel-runner.js 执行超时（>{_RUNNER_TIMEOUT}s），flow={flow}"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                "未找到 Node 可执行文件，请确认已安装 Node.js 并加入 PATH，"
                "或通过 NODE_EXECUTABLE 环境变量指定绝对路径。"
            ) from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            stdout = (proc.stdout or "").strip()
            raise RuntimeError(
                f"sentinel-runner.js 退出码 {proc.returncode}\n"
                f"stderr: {stderr}\n"
                f"stdout: {stdout}"
            )

        token_text = (proc.stdout or "").strip()
        if not token_text:
            raise RuntimeError(
                f"sentinel-runner.js 输出为空, stderr: {(proc.stderr or '').strip()}"
            )

        try:
            parsed = json.loads(token_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"runner 输出不是合法 JSON: {token_text[:200]}"
            ) from exc

        for required_key in ("p", "c", "id", "flow"):
            if required_key not in parsed:
                raise RuntimeError(
                    f"runner 输出缺少字段 {required_key}: {token_text[:200]}"
                )

        field_summary = {
            k: (len(v) if isinstance(v, str) else type(v).__name__)
            for k, v in parsed.items()
        }
        logger.info(
            f"[SentinelRunner] token 生成成功, flow={flow}, "
            f"包含 turnstile={'t' in parsed and bool(parsed.get('t'))}, "
            f"包含 so={bool(parsed.get('so'))}, "
            f"字段: {field_summary}"
        )
        return token_text

    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
