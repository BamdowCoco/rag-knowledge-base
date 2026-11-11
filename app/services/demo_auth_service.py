"""
访客表单登录的纯逻辑：账号文件、bcrypt 校验、HMAC 会话令牌、失败限流。

**为什么不复用 Caddy 的 Basic Auth**：浏览器接管 401 登录框，无法显示“密码错误”，
也无法在弹窗里稳定展示帮助信息。表单登录把校验放到应用侧，错误文案和退出登录都可控。

账号文件仍是 ``deploy/caddy/guests.caddy``（``用户名 bcrypt-hash``），由
``deploy/guest_accounts.sh`` 维护；Caddy 不再 import 它，只有本模块读取。
"""
import base64
import binascii
import hashlib
import hmac
import logging
import re
import secrets
import time
from pathlib import Path

import bcrypt

logger = logging.getLogger(__name__)

# 用户名会进入 Cookie 和日志，限制字符集，避免冒号/换行破坏令牌格式
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 会话密钥为空时的兜底：随机生成并复用；重启后失效，生产应配置 DEMO_SESSION_SECRET
_fallback_secret: str | None = None


def get_session_secret(configured: str) -> str:
    """
    返回 Cookie 签名密钥。

    :param configured: ``settings.demo_session_secret``；为空时使用进程内随机值。
    :return: 非空密钥。
    """
    global _fallback_secret
    if configured:
        return configured
    if _fallback_secret is None:
        _fallback_secret = secrets.token_hex(32)
        logger.warning(
            "DEMO_SESSION_SECRET 未配置，已生成进程内随机密钥；重启后访客会话会失效"
        )
    return _fallback_secret


def load_accounts(path: str) -> dict[str, str]:
    """
    读取 Caddy 格式的访客账号文件。

    每行 ``<用户名> <bcrypt hash>``；空行和 ``#`` 注释跳过，非法行记日志后忽略。

    :param path: 账号文件路径。
    :return: ``{用户名: bcrypt hash}``。
    :raises OSError: 文件不存在或不可读。
    """
    accounts: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            logger.warning("访客账号文件第 %s 行格式错误，已忽略", lineno)
            continue
        username, hashed = parts
        if not USERNAME_RE.match(username):
            logger.warning("访客账号文件第 %s 行用户名非法，已忽略", lineno)
            continue
        accounts[username] = hashed
    return accounts


def verify_password(password: str, hashed: str) -> bool:
    """
    校验明文密码与 bcrypt hash。

    :param password: 用户输入的密码。
    :param hashed: ``$2b$...`` 格式的 bcrypt hash。
    :return: 是否匹配；非法 hash 返回 False。
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        logger.warning("访客账号 hash 非法，无法校验")
        return False


def sign_session(
    secret: str,
    username: str,
    ttl_seconds: int,
    *,
    now: float | None = None,
) -> str:
    """
    生成 ``base64url(username:expires:hmac)`` 形式的会话令牌。

    :param secret: HMAC 密钥。
    :param username: 已通过密码校验的用户名。
    :param ttl_seconds: 有效期秒数。
    :param now: 当前时间（测试用）。
    :return: 可直接放进 Cookie 的令牌。
    """
    expires = int(now if now is not None else time.time()) + int(ttl_seconds)
    payload = f"{username}:{expires}"
    signature = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    raw = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_session(
    secret: str,
    token: str | None,
    *,
    now: float | None = None,
) -> str | None:
    """
    校验会话令牌。

    :param secret: HMAC 密钥。
    :param token: Cookie 里的令牌。
    :param now: 当前时间（测试用）。
    :return: 有效时返回用户名，否则 ``None``。
    """
    if not secret or not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        username, expires_text, signature = raw.rsplit(":", 2)
        expires = int(expires_text)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if not USERNAME_RE.match(username):
        return None
    payload = f"{username}:{expires}"
    expected = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    if expires <= int(now if now is not None else time.time()):
        return None
    return username


class LoginLimiter:
    """
    内存级登录失败限流。

    单进程有效；多 worker 时每个进程各自计数。对演示站点足够，重启后计数清零。
    """

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        """
        :param max_failures: 窗口内允许的失败次数。
        :param window_seconds: 统计窗口秒数。
        """
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, tuple[int, float]] = {}

    def _now(self, now: float | None) -> float:
        return now if now is not None else time.monotonic()

    def _prune(self, now: float) -> None:
        """清理过期 key，避免长期运行后字典无限增长。"""
        expired = [
            key
            for key, (_, started) in self._failures.items()
            if now - started >= self.window_seconds
        ]
        for key in expired:
            self._failures.pop(key, None)

    def is_blocked(self, key: str, *, now: float | None = None) -> bool:
        """判断 key 是否已超过失败上限。"""
        current = self._now(now)
        self._prune(current)
        count, started = self._failures.get(key, (0, current))
        if current - started >= self.window_seconds:
            return False
        return count >= self.max_failures

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        """记录一次失败；窗口过期则重新计数。"""
        current = self._now(now)
        count, started = self._failures.get(key, (0, current))
        if current - started >= self.window_seconds:
            count, started = 0, current
        self._failures[key] = (count + 1, started)

    def reset(self, key: str) -> None:
        """登录成功后清除该 key 的失败计数。"""
        self._failures.pop(key, None)
