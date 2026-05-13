# -*- coding: utf-8 -*-
"""
Outlook 邮箱客户端（mail.chatai.codes 双协议）

账号文件格式（每行一个）：
    email----password----clientId----refreshToken
例：
    SorenBarrett5150@outlook.com----oc621409----9e5f94bc-...----M.C529_...

工作流：
    1. pick_account()       从根目录 `用于注册的邮箱.json` 中挑一个未用过的账号
    2. fetch_latest_otp()   双协议（Graph / IMAP）轮询取 OTP
    3. 注册成功后会写入 `注册成功的邮箱.txt` 与 `注册成功的token.txt`

只用 Outlook 提供的 refresh_token 调远端的 mail.chatai.codes 服务，
不直连 Microsoft Graph，因为后者要 access_token + 复杂 OAuth 协议。
"""
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from curl_cffi.requests import Session as CurlSession

from config import (
    OUTLOOK_ACCOUNTS_FILE,
    OUTLOOK_API_BASE,
    OTP_POLL_INTERVAL,
    OTP_MAX_WAIT,
    OTP_SETTLE_SECONDS,
    USER_AGENT,
    pick_proxy,
)
from core.otp_utils import looks_like_openai_email, extract_otp

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 邮箱 → account 上下文的内存缓存，fetch_latest_otp 用
_CONTEXT_CACHE: dict[str, "OutlookAccount"] = {}


@dataclass
class OutlookAccount:
    email: str
    password: str
    client_id: str
    refresh_token: str


class OutlookClientError(RuntimeError):
    """Outlook 邮箱服务相关异常。"""


def _http_session() -> CurlSession:
    s = CurlSession(impersonate="chrome142")
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Origin": OUTLOOK_API_BASE.rstrip("/"),
        "Referer": OUTLOOK_API_BASE.rstrip("/") + "/",
        "Accept": "*/*",
    })
    s.timeout = 30
    proxy = pick_proxy()
    if proxy:
        s.proxies = {
            "http": proxy,
            "https": proxy,
        }
    return s


# ============================================================
# 账号文件读写
# ============================================================

def _parse_accounts_file(path: Path) -> list[OutlookAccount]:
    """从纯文本文件解析账号，仅在 import_to_db 时使用。"""
    if not path.exists():
        return []
    accounts: list[OutlookAccount] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) != 4:
            logger.warning(
                f"[Outlook] {path.name} 第 {lineno} 行格式不符（期望 4 段，实际 {len(parts)}），已跳过"
            )
            continue
        email, password, client_id, refresh_token = (p.strip() for p in parts)
        accounts.append(OutlookAccount(email, password, client_id, refresh_token))
    return accounts


# ============================================================
# 公共接口：挑账号 / 取 OTP（统一走 DB）
# ============================================================

def pick_account() -> OutlookAccount:
    """
    原子地挑一个 status='available' 的 Outlook 账号并标记为 'used'（DB 事务）。
    多线程并发安全。
    """
    from core.db import claim_next_outlook, outlook_pool_summary

    inserted, skipped = import_outlook_from_file()
    if inserted:
        logger.info(f"[Outlook] 已自动从 {OUTLOOK_ACCOUNTS_FILE} 导入 {inserted} 个新账号（跳过 {skipped} 个）")

    row = claim_next_outlook()
    if row is None:
        summary = outlook_pool_summary()
        raise OutlookClientError(
            f"Outlook 账号池没有可用账号: {summary}. "
            f"请把新邮箱写入 {OUTLOOK_ACCOUNTS_FILE}，程序会在下次注册前自动导入。"
        )

    account = OutlookAccount(
        email=row["email"],
        password=row["password"],
        client_id=row["client_id"],
        refresh_token=row["refresh_token"],
    )
    _CONTEXT_CACHE[account.email] = account
    logger.info(f"[Outlook] 选中账号: {account.email}（DB id={row['id']}）")
    return account


def get_account_context(email: str) -> OutlookAccount | None:
    """根据邮箱查 OutlookAccount 上下文。优先内存缓存，fallback 查 DB。"""
    if email in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[email]
    from core.db import get_outlook_by_email
    row = get_outlook_by_email(email)
    if row is None:
        return None
    account = OutlookAccount(
        email=row["email"],
        password=row["password"],
        client_id=row["client_id"],
        refresh_token=row["refresh_token"],
    )
    _CONTEXT_CACHE[email] = account
    return account


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """按注册阶段结果更新 Outlook 账号状态：可重试回 available，已消耗则标记 failed。"""
    from core.db import release_outlook
    release_outlook(email, status=status, note=note)
    _CONTEXT_CACHE.pop(email, None)


def import_outlook_from_file(path: str | Path | None = None) -> tuple[int, int]:
    """读取一份账号文本文件，全量导入 DB，返回 (新增, 已存在跳过)。"""
    from core.db import import_outlook_accounts
    p = Path(path or OUTLOOK_ACCOUNTS_FILE)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    accounts = _parse_accounts_file(p)
    records = [
        {"email": a.email, "password": a.password, "client_id": a.client_id, "refresh_token": a.refresh_token}
        for a in accounts
    ]
    return import_outlook_accounts(records)


def import_outlook_from_text(text: str) -> tuple[int, int]:
    """直接给一段多行文本（粘贴用），导入 DB。"""
    from core.db import import_outlook_accounts
    records = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) != 4:
            continue
        email, password, client_id, refresh_token = (p.strip() for p in parts)
        records.append({
            "email": email, "password": password,
            "client_id": client_id, "refresh_token": refresh_token,
        })
    return import_outlook_accounts(records)


# ============================================================
# 抓取邮件：Graph 失败回退 IMAP
# ============================================================

def _fetch_via(session: CurlSession, protocol: str, account: OutlookAccount) -> list[dict]:
    """
    调用 mail.chatai.codes 拉收件箱，返回 emails 列表。
    protocol 取 "graph" 或 "imap"。
    """
    url = f"{OUTLOOK_API_BASE.rstrip('/')}/api/fetch-{protocol}"
    payload = {
        "email": account.email,
        "clientId": account.client_id,
        "refreshToken": account.refresh_token,
        "keyword": "",
        "limit": 10,
        "sender": "",
    }
    headers = {"Content-Type": "application/json"}
    try:
        resp = session.post(url, headers=headers, data=json.dumps(payload))
    except Exception as exc:
        logger.warning(f"[Outlook] {protocol} 请求异常: {type(exc).__name__}: {exc}")
        return []

    if resp.status_code != 200:
        logger.warning(f"[Outlook] {protocol} HTTP {resp.status_code}: {resp.text[:200]}")
        return []

    try:
        data = resp.json()
    except Exception as exc:
        logger.warning(f"[Outlook] {protocol} 响应不是 JSON: {exc}, body={resp.text[:200]}")
        return []

    if not data.get("success"):
        # graph 偶尔会返回 {"success":false,"error":"Unexpected end of JSON input"}
        # 不算致命，让外层回退另一协议
        logger.debug(f"[Outlook] {protocol} success=False: {data.get('error')}")
        return []

    emails = data.get("emails") or []
    logger.debug(f"[Outlook] {protocol} 拿到 {len(emails)} 封邮件")
    return emails


# settle 机制默认值改为从 config 读取（OTP_SETTLE_SECONDS）
# 抓到第一封 OTP 后，再多等多少秒看是否有更晚到的邮件。
# 看到更晚的就重置 settle 计时；连续无新邮件 settle 秒后才返回。


def fetch_otp_with_account(
    account: OutlookAccount,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    subject_includes: list[str] | None = None,
    subject_excludes: list[str] | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    直接给定 OutlookAccount（含 client_id / refresh_token）拉 OTP。
    适用于 account 不在 DB / 不在内存缓存的场景（如外部脚本调用）。
    """
    _CONTEXT_CACHE[account.email] = account
    return fetch_latest_otp(
        account.email,
        after_ts=after_ts,
        max_wait=max_wait,
        poll_interval=poll_interval,
        subject_includes=subject_includes,
        subject_excludes=subject_excludes,
        settle_seconds=settle_seconds,
    )


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    subject_includes: list[str] | None = None,
    subject_excludes: list[str] | None = None,
    settle_seconds: int | None = None,
) -> str:
    """
    双协议轮询取 OTP，规则：
        - 先试 Graph，失败/为空时回退 IMAP
        - 把两边返回的邮件合并去重，按时间降序排序
        - 取**最新**一封 OpenAI 邮件抽 OTP
        - **settle 机制**：抓到第一封后再等 settle_seconds 看是否有更晚到的，
          有就用最新的；无新邮件后才返回。避免抓到途中那封被服务端更新的旧 OTP。

    Args:
        email: 目标邮箱
        after_ts: UTC 时间戳。只看比这个时间新的邮件
        max_wait / poll_interval: 默认走 config 里的值
        subject_includes / subject_excludes: 可选 subject 过滤
        settle_seconds: 抓到第一封后再等多少秒看有没有更新的（默认 8s）
    """
    account = get_account_context(email)
    if account is None:
        raise OutlookClientError(f"未找到 {email} 的账号上下文，无法取 OTP")

    deadline = time.time() + (max_wait or OTP_MAX_WAIT)
    interval = poll_interval or OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else OTP_SETTLE_SECONDS
    session = _http_session()

    logger.info(
        f"[Outlook] 开始轮询 {email} 的收件箱（双协议 graph + imap），"
        f"最长 {max_wait or OTP_MAX_WAIT}s, settle={settle}s..."
    )

    # settle 状态机
    best_otp: str | None = None       # 当前看到的最新 OTP
    best_ts: float = 0.0              # 它的邮件时间戳
    best_subject: str = ""
    best_protocol: str = ""
    settle_until: float | None = None # 抓到第一封后，等到这个时刻才返回

    while time.time() < deadline:
        # 每轮都重新拉，因为可能有新邮件，也可能旧邮件因延迟才出现
        all_candidates: list[tuple[str, dict, float]] = []
        for protocol in ("graph", "imap"):
            emails = _fetch_via(session, protocol, account)
            for item in emails:
                ts = _parse_email_ts(item) or 0.0
                all_candidates.append((protocol, item, ts))

        # 按时间降序，最新的在前
        all_candidates.sort(key=lambda x: x[2], reverse=True)

        # 找出本轮"最新一封通过过滤的 OpenAI 邮件"
        for protocol, item, ts in all_candidates:
            if not looks_like_openai_email(item):
                continue

            subject = (item.get("subject") or "")
            subject_lower = subject.lower()
            if subject_includes and not any(s.lower() in subject_lower for s in subject_includes):
                continue
            if subject_excludes and any(s.lower() in subject_lower for s in subject_excludes):
                continue
            if after_ts is not None and not _is_after(item, after_ts):
                continue

            otp = extract_otp(item)
            if not otp:
                continue

            # 已锁定一个候选；如果新看到的更晚，则替换并重置 settle 倒计时
            if ts > best_ts:
                if best_otp:
                    logger.info(
                        f"[Outlook] 发现更晚的 OTP={otp} (ts={item.get('date') or item.get('receivedDateTime')}), "
                        f"替换之前的 {best_otp}, 重置 settle 计时"
                    )
                else:
                    logger.info(
                        f"[Outlook] 首次锁定 OTP={otp}, ts={item.get('date') or item.get('receivedDateTime')}, "
                        f"subject={subject!r}, 等 {settle}s 看是否有更晚邮件..."
                    )
                best_otp = otp
                best_ts = ts
                best_subject = subject
                best_protocol = protocol
                settle_until = time.time() + settle
            break  # 只关心本轮最新那一封

        # 判断是否可以返回
        now = time.time()
        if best_otp and settle_until is not None and now >= settle_until:
            logger.info(
                f"[Outlook] settle 完成，返回 OTP={best_otp}, protocol={best_protocol}, "
                f"subject={best_subject!r}"
            )
            return best_otp

        remaining = int(deadline - now)
        if best_otp:
            logger.info(
                f"[Outlook] 已锁定候选 OTP={best_otp}，等 settle 中"
                f"（剩余 settle ~{int(settle_until - now)}s, 总剩余 {remaining}s）..."
            )
        else:
            logger.info(
                f"[Outlook] 暂未收到符合条件的 OpenAI 邮件，{interval}s 后重试（剩余 {remaining}s）..."
            )
        time.sleep(interval)

    # 超时但已经锁定过候选（settle 没等到结束就到 deadline 了）
    if best_otp:
        logger.warning(
            f"[Outlook] 总超时但已有候选，返回 OTP={best_otp} (subject={best_subject!r})"
        )
        return best_otp

    raise OutlookClientError(
        f"等待 {email} 的 OTP 超时（>{max_wait or OTP_MAX_WAIT}s）。"
        f"可能：refresh_token 失效 / 邮箱被 OpenAI 黑名单 / IP 风控未通过。"
    )


# 时差容忍：仅 30 秒（足以吸收客户端/邮件服务器 NTP 偏差）。
# 不能像之前那样放 5 分钟——OTP 30 秒就轮换一次，旧 OTP 会被误判通过。
_OTP_CLOCK_SKEW_TOLERANCE = 30


def _parse_email_ts(item: dict) -> float | None:
    """把邮件的时间字段解析成 UTC 时间戳；解析不出返回 None。"""
    import calendar
    raw = (
        item.get("date")
        or item.get("receivedDateTime")
        or item.get("createTime")
        or item.get("receivedAt")
        or ""
    )
    if not raw:
        return None

    formats = (
        "%Y-%m-%dT%H:%M:%SZ",       # Graph: 2026-05-08T02:47:00Z
        "%Y-%m-%dT%H:%M:%S.%fZ",    # Graph 含微秒
        "%Y-%m-%d %H:%M:%S",        # IMAP / 自定义
        "%a, %d %b %Y %H:%M:%S %z", # RFC 2822 with tz
    )
    for fmt in formats:
        try:
            if fmt.endswith("%z"):
                from datetime import datetime
                return datetime.strptime(raw, fmt).timestamp()
            base_fmt = fmt[: fmt.index("%f") - 1] if "%f" in fmt else fmt
            return float(calendar.timegm(time.strptime(raw[:19] if len(raw) >= 19 else raw, base_fmt)))
        except Exception:
            continue
    return None


def _is_after(item: dict, after_ts: float) -> bool:
    """判断邮件时间是否晚于 after_ts。容忍仅 30 秒以避免吃到旧 OTP。"""
    ts = _parse_email_ts(item)
    if ts is None:
        # 时间字段缺失/解析不出 → 放过（不要因解析失败就丢邮件）
        return True
    return ts >= after_ts - _OTP_CLOCK_SKEW_TOLERANCE
