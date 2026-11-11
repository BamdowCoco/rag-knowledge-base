"""访客表单登录的纯逻辑：账号文件、bcrypt、HMAC 会话、失败限流。

这些测试不碰 FastAPI / DB；登录接口的行为在 ``tests/api/test_demo_auth.py``。
"""
import bcrypt

from app.services.demo_auth_service import (
    LoginLimiter,
    load_accounts,
    sign_session,
    verify_password,
    verify_session,
)


def _hash(password: str) -> str:
    """生成低 cost 的 bcrypt hash，测试跑得快。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode()


def test_load_accounts_skips_comments_and_malformed_lines(tmp_path):
    """注释、空行、单字段/三字段行都跳过，只保留合法账号。"""
    path = tmp_path / "guests.caddy"
    path.write_text(
        "# comment\n"
        "\n"
        "demo $2b$14$hash1\n"
        "u1a2b3c4 $2b$14$hash2\n"
        "onlyone\n"
        "u2 $2b$14$hash extra\n",
        encoding="utf-8",
    )

    accounts = load_accounts(str(path))

    assert accounts == {"demo": "$2b$14$hash1", "u1a2b3c4": "$2b$14$hash2"}


def test_verify_password_accepts_correct_and_rejects_wrong():
    """bcrypt 校验成功/失败；非法 hash 不抛异常。"""
    hashed = _hash("correct-horse")

    assert verify_password("correct-horse", hashed) is True
    assert verify_password("wrong", hashed) is False
    assert verify_password("correct-horse", "not-a-bcrypt-hash") is False


def test_session_roundtrip_and_expiry():
    """签名令牌可验证、过期后失效。"""
    token = sign_session("secret", "demo", 60, now=1000)

    assert verify_session("secret", token, now=1001) == "demo"
    assert verify_session("secret", token, now=1059) == "demo"
    assert verify_session("secret", token, now=1060) is None


def test_session_rejects_tampering_and_wrong_secret():
    """篡改令牌或换密钥都应失败。"""
    token = sign_session("secret", "demo", 60, now=1000)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")

    assert verify_session("secret", tampered, now=1001) is None
    assert verify_session("other-secret", token, now=1001) is None


def test_session_rejects_invalid_username():
    """用户名不符合字符集时，即使签名正确也不接受。"""
    token = sign_session("secret", "bad name", 60, now=1000)

    assert verify_session("secret", token, now=1001) is None


def test_login_limiter_blocks_and_recovers_after_window():
    """连续失败到上限后拦截，窗口过期后恢复；成功可清零。"""
    limiter = LoginLimiter(max_failures=2, window_seconds=60)

    assert limiter.is_blocked("ip:demo", now=0) is False
    limiter.record_failure("ip:demo", now=0)
    limiter.record_failure("ip:demo", now=1)

    assert limiter.is_blocked("ip:demo", now=2) is True
    assert limiter.is_blocked("ip:demo", now=62) is False

    limiter.record_failure("ip:demo", now=100)
    limiter.reset("ip:demo")
    assert limiter.is_blocked("ip:demo", now=101) is False
