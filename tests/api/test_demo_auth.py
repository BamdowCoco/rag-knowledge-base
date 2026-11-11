"""访客表单登录接口：登录、Cookie 校验、退出、失败限流。

账号文件用临时文件，Cookie 在测试里关掉 Secure（测试客户端走 http://test）。
"""
import bcrypt
import pytest

from app.api.v1.endpoints import demo_auth
from app.core.config import settings

BASE = "/api/v1/auth"


# 登录测试只用临时账号文件，不碰 MySQL / Redis / Qdrant；
# 覆盖根 conftest 的 session 级脚手架，便于单独跑这个文件。
@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    """覆盖根 conftest：登录测试不建测试库。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def prepare_qdrant():
    """覆盖根 conftest：登录测试不建 Qdrant 集合。"""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """覆盖根 conftest：登录测试不需要清库 / 清 Redis。"""
    yield


def _hash(password: str) -> str:
    """低 cost bcrypt，测试跑得快。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=4)).decode()


async def _login(client) -> str:
    """登录并返回会话令牌。

    Cookie 的 Path 是 ``/demo``；测试直接访问 ``/api/v1/auth/check`` 时，
    httpx 的 cookie jar 不会自动带上它。生产里由 Caddy 把浏览器发到 ``/demo/*`` 的
    Cookie 转给 auth 子请求，所以测试里显式放进 ``Cookie`` 头模拟这一步。
    """
    response = await client.post(
        f"{BASE}/login", json={"username": "demo", "password": "secret"}
    )
    assert response.status_code == 200
    return response.cookies["rag_demo_session"]


@pytest.fixture(autouse=True)
def demo_accounts(monkeypatch, tmp_path, client):
    """每个测试写一份独立账号文件，并重置会话/限流状态。"""
    path = tmp_path / "guests.caddy"
    path.write_text(f"demo {_hash('secret')}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "demo_accounts_file", str(path))
    monkeypatch.setattr(settings, "demo_session_secret", "test-secret")
    monkeypatch.setattr(settings, "demo_cookie_secure", False)
    demo_auth._limiter._failures.clear()  # noqa: SLF001 - 测试里重置模块级限流器
    client.cookies.clear()
    yield path
    demo_auth._limiter._failures.clear()  # noqa: SLF001
    client.cookies.clear()


async def test_login_success_sets_cookie(client):
    """正确密码返回 200，并设置会话 Cookie。"""
    response = await client.post(
        f"{BASE}/login", json={"username": "demo", "password": "secret"}
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "user": "demo"}
    assert "rag_demo_session" in response.cookies


async def test_login_wrong_password_returns_401(client):
    """错误密码返回 401 和明确文案。"""
    response = await client.post(
        f"{BASE}/login", json={"username": "demo", "password": "wrong"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "用户名或密码错误。"


async def test_check_requires_and_accepts_cookie(client):
    """无 Cookie 302 到登录页；登录后 check 返回用户名。"""
    unauthorized = await client.get(f"{BASE}/check")
    assert unauthorized.status_code == 302
    assert unauthorized.headers["location"].startswith("/demo/login")

    token = await _login(client)
    authorized = await client.get(
        f"{BASE}/check", headers={"Cookie": f"rag_demo_session={token}"}
    )

    assert authorized.status_code == 200
    assert authorized.headers["x-demo-user"] == "demo"


async def test_logout_clears_cookie(client):
    """退出返回 303，并通过 Set-Cookie 删除会话。"""
    token = await _login(client)

    response = await client.get(
        f"{BASE}/logout", headers={"Cookie": f"rag_demo_session={token}"}
    )

    assert response.status_code == 303
    set_cookie = response.headers.get("set-cookie", "")
    assert "rag_demo_session=" in set_cookie
    assert "Max-Age=0" in set_cookie


async def test_removed_account_invalidates_existing_cookie(client, demo_accounts):
    """账号从文件删除后，旧 Cookie 立即失效。"""
    token = await _login(client)
    demo_accounts.write_text("# no accounts\n", encoding="utf-8")

    response = await client.get(
        f"{BASE}/check", headers={"Cookie": f"rag_demo_session={token}"}
    )

    assert response.status_code == 302


async def test_login_rate_limit_returns_429(client):
    """连续失败达到上限后返回 429。"""
    for _ in range(settings.demo_login_max_failures):
        response = await client.post(
            f"{BASE}/login", json={"username": "demo", "password": "wrong"}
        )
        assert response.status_code == 401

    blocked = await client.post(
        f"{BASE}/login", json={"username": "demo", "password": "wrong"}
    )

    assert blocked.status_code == 429
