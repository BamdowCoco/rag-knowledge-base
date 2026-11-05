"""
CORS 白名单：只有配置里列出的来源才拿得到 ``access-control-allow-origin``。

演示时聊天页/运维页由 rag-api（:8001）同源提供，运维页跨源调 api（:8000）的统计接口；
浏览器先发预检，所以这里盯的就是预检响应头。用**非白名单来源**做反例，防止哪天
配置被改成 ``*`` 或中间件顺序错乱却没人发现。
"""
from app.core.config import Settings

_ALLOWED = "http://localhost:8001"
_DISALLOWED = "http://evil.example"


async def test_preflight_allowed_origin(client):
    """白名单来源的预检返回对应的 allow-origin。"""
    response = await client.options(
        "/api/v1/documents/stats",
        headers={"Origin": _ALLOWED, "Access-Control-Request-Method": "GET"},
    )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == _ALLOWED


async def test_preflight_disallowed_origin(client):
    """不在白名单里的来源拿不到 allow-origin（预检被拒，页面侧会看到 CORS 错误）。"""
    response = await client.options(
        "/api/v1/documents/stats",
        headers={"Origin": _DISALLOWED, "Access-Control-Request-Method": "GET"},
    )

    assert response.headers.get("access-control-allow-origin") is None


def test_default_origins_are_not_wildcard():
    """默认配置必须是具体来源列表，而不是 ``*`` —— 白名单是这一层的意义所在。"""
    # 读类字段的默认值，不受 .env / 环境变量覆盖影响
    default = Settings.model_fields["cors_origins"].default
    origins = [origin.strip() for origin in default.split(",")]

    assert "*" not in origins
    assert "http://localhost:8001" in origins


def test_origin_list_parsing_ignores_blank_items():
    """``cors_origin_list`` 按逗号切分，去掉首尾空白与空项。"""
    parsed = Settings(cors_origins=" http://a.example ,, http://b.example ").cors_origin_list

    assert parsed == ["http://a.example", "http://b.example"]
