"""
``crawler.fetch`` 的单元测试：条件请求、状态映射、重试、robots 与限速。

全部用 ``httpx.MockTransport`` ，不访问真实网络；退避等待通过 monkeypatch
模块级 ``_sleep`` 记录，测试不会真等 1 秒。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Callable

import httpx
import pytest

import crawler.fetch as fetch_module
from crawler.fetch import HostRateLimiter, RobotsRules, fetch_bytes, fetch_page

PAGE_URL = "https://example.com/zh/tutorial/dependencies/"


def make_cfg(**overrides: Any) -> SimpleNamespace:
    """
    构造 fetch 层用到的最小配置。

    :param overrides: 需要覆盖的字段。
    :return: 带属性的简单对象（``fetch`` 只按属性读取，不依赖具体类型）。
    """
    values: dict[str, Any] = {
        "user_agent": "rag-ingest-demo/0.1 (+https://github.com/BamdowCoco/rag-ingest-pipeline)",
        "max_retries": 2,
        "timeout_seconds": 5.0,
        "max_page_bytes": 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """
    用 ``MockTransport`` 构造测试客户端。

    :param handler: 请求处理函数。
    :return: 尚未进入上下文的 ``AsyncClient`` 。
    """
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """
    把 ``_sleep`` 换成只记录参数的假实现。

    :return: 被记录的等待秒数列表。
    """
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(fetch_module, "_sleep", fake_sleep)
    return recorded


async def test_conditional_request_gets_304_and_echoes_validators() -> None:
    """200 带 ETag/Last-Modified；下次带条件头得到 304，并沿用缓存值。"""
    seen_headers: list[dict[str, str]] = []
    last_modified = "Wed, 01 Oct 2026 10:00:00 GMT"

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if (
            request.headers.get("if-none-match") == '"v1"'
            and request.headers.get("if-modified-since") == last_modified
        ):
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return httpx.Response(
            200,
            text="<html><body>正文</body></html>",
            headers={"ETag": '"v1"', "Last-Modified": last_modified},
        )

    cfg = make_cfg()
    limiter = HostRateLimiter(concurrency=1, delay=0)

    async with make_client(handler) as client:
        first = await fetch_page(client, PAGE_URL, cfg=cfg, limiter=limiter)
        assert first.kind == "ok"
        assert first.status_code == 200
        assert "正文" in (first.text or "")
        assert first.etag == '"v1"'
        assert first.last_modified == last_modified

        second = await fetch_page(
            client,
            PAGE_URL,
            cfg=cfg,
            limiter=limiter,
            etag=first.etag,
            last_modified=first.last_modified,
        )

    assert second.kind == "not_modified"
    assert second.status_code == 304
    assert second.text is None
    assert second.etag == '"v1"'
    assert second.last_modified == last_modified

    assert seen_headers[0]["user-agent"] == cfg.user_agent
    assert "if-none-match" not in seen_headers[0]
    assert seen_headers[1]["if-none-match"] == '"v1"'
    assert seen_headers[1]["if-modified-since"] == last_modified


async def test_replay_mode_drops_conditional_headers() -> None:
    """``replay=True`` 时不带 ``If-None-Match`` / ``If-Modified-Since`` 。"""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, text="<html>正文</html>")

    async with make_client(handler) as client:
        result = await fetch_page(
            client,
            PAGE_URL,
            cfg=make_cfg(),
            limiter=HostRateLimiter(1, 0),
            etag='"v1"',
            last_modified="Wed, 01 Oct 2026 10:00:00 GMT",
            replay=True,
        )

    assert result.kind == "ok"
    assert "if-none-match" not in captured
    assert "if-modified-since" not in captured


@pytest.mark.parametrize("status", [404, 410])
async def test_missing_page_maps_to_gone(status: int) -> None:
    """``404`` / ``410`` 映射为 ``gone`` ，由调用方发删除信号。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async with make_client(handler) as client:
        result = await fetch_page(client, PAGE_URL, cfg=make_cfg(), limiter=HostRateLimiter(1, 0))

    assert result.kind == "gone"
    assert result.status_code == status
    assert result.text is None


async def test_429_honors_retry_after_then_succeeds(sleeps: list[float]) -> None:
    """``429`` 读 ``Retry-After`` ，等待后重试成功。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, text="<html>ok</html>")

    async with make_client(handler) as client:
        result = await fetch_page(client, PAGE_URL, cfg=make_cfg(), limiter=HostRateLimiter(1, 0))

    assert result.kind == "ok"
    assert calls == 2
    assert sleeps == [2.0]


async def test_5xx_retries_with_exponential_backoff(
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``5xx`` 按 ``1s → 2s → 4s`` 退避，重试次数用尽后返回 ``error`` 。"""
    monkeypatch.setattr(fetch_module.random, "uniform", lambda low, high: 0.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    cfg = make_cfg(max_retries=3)
    async with make_client(handler) as client:
        result = await fetch_page(client, PAGE_URL, cfg=cfg, limiter=HostRateLimiter(1, 0))

    assert result.kind == "error"
    assert result.status_code == 503
    # 首次请求 + 3 次重试
    assert calls == 4
    assert sleeps == [1.0, 2.0, 4.0]


async def test_4xx_other_than_404_410_is_not_retried(sleeps: list[float]) -> None:
    """``403`` 等其余 4xx 不重试。"""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    async with make_client(handler) as client:
        result = await fetch_page(client, PAGE_URL, cfg=make_cfg(), limiter=HostRateLimiter(1, 0))

    assert result.kind == "error"
    assert result.status_code == 403
    assert calls == 1
    assert sleeps == []


async def test_declared_content_length_over_limit_is_skipped() -> None:
    """``Content-Length`` 超限时不读正文，直接 ``skipped`` 。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "5000"}, content=b"tiny")

    cfg = make_cfg(max_page_bytes=100)
    async with make_client(handler) as client:
        result = await fetch_page(client, PAGE_URL, cfg=cfg, limiter=HostRateLimiter(1, 0))

    assert result.kind == "skipped"
    assert result.status_code == 200
    assert "Content-Length" in (result.reason or "")


async def test_actual_content_over_limit_is_skipped() -> None:
    """没有 ``Content-Length`` 时，按实际读到的字节数兜底检查。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 200))

    cfg = make_cfg(max_page_bytes=100)
    async with make_client(handler) as client:
        result = await fetch_page(client, PAGE_URL, cfg=cfg, limiter=HostRateLimiter(1, 0))

    assert result.kind == "skipped"
    assert "字节" in (result.reason or "")


async def test_network_error_retries_then_error(
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网络异常按 5xx 同样的规则退避重试，最终映射为 ``error`` 。"""
    monkeypatch.setattr(fetch_module.random, "uniform", lambda low, high: 0.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom", request=request)

    cfg = make_cfg(max_retries=2)
    async with make_client(handler) as client:
        result = await fetch_page(client, PAGE_URL, cfg=cfg, limiter=HostRateLimiter(1, 0))

    assert result.kind == "error"
    assert result.status_code is None
    assert calls == 3
    assert sleeps == [1.0, 2.0]
    assert "网络异常" in (result.reason or "")


async def test_fetch_bytes_returns_payload_and_enforces_limit() -> None:
    """``fetch_bytes`` 返回原始字节；超过 ``max_bytes`` 抛 ``RuntimeError`` 。"""
    payload = b"User-agent: *\nDisallow:\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    async with make_client(handler) as client:
        data = await fetch_bytes(
            client,
            "https://example.com/robots.txt",
            cfg=make_cfg(),
            limiter=HostRateLimiter(1, 0),
            max_bytes=1024,
        )

    assert data == payload

    def oversized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 200))

    async with make_client(oversized) as client:
        with pytest.raises(RuntimeError):
            await fetch_bytes(
                client,
                "https://example.com/sitemap.xml",
                cfg=make_cfg(),
                limiter=HostRateLimiter(1, 0),
                max_bytes=100,
            )


def test_robots_empty_or_comment_only_allows_all() -> None:
    """没有生效指令（例如目标站 robots 全是注释）时默认允许。"""
    rules = RobotsRules.parse("# 全是注释\n\n", "rag-ingest-demo/0.1")

    assert rules.allowed("https://example.com/zh/") is True
    assert rules.crawl_delay is None


def test_robots_longest_match_and_crawl_delay() -> None:
    """``Allow`` / ``Disallow`` 按最长前缀匹配；``Crawl-delay`` 生效。"""
    text = """
User-agent: *
Disallow: /private/
Allow: /private/public/
Crawl-delay: 2
"""
    rules = RobotsRules.parse(text, "rag-ingest-demo/0.1")

    assert rules.allowed("https://example.com/private/x") is False
    assert rules.allowed("https://example.com/private/public/x") is True
    assert rules.allowed("https://example.com/zh/") is True
    assert rules.crawl_delay == 2.0


def test_robots_specific_user_agent_overrides_star() -> None:
    """有更匹配的 UA 组时，不用 ``*`` 组的规则（大小写不敏感）。"""
    text = """
User-agent: *
Disallow: /

User-agent: rag-ingest-demo
Disallow: /admin/
"""
    rules = RobotsRules.parse(text, "Rag-Ingest-Demo/0.1 (+https://github.com/BamdowCoco/rag-ingest-pipeline)")

    assert rules.allowed("https://example.com/zh/") is True
    assert rules.allowed("https://example.com/admin/x") is False


def test_robots_wildcard_and_end_anchor() -> None:
    """支持 ``*`` 通配与结尾 ``$`` 锚定。"""
    text = """
User-agent: rag-ingest-demo
Disallow: /*/secret$
Crawl-delay: 1.5
"""
    rules = RobotsRules.parse(text, "rag-ingest-demo/0.1")

    assert rules.allowed("https://example.com/zh/secret") is False
    assert rules.allowed("https://example.com/zh/secret/page") is True
    assert rules.crawl_delay == 1.5


def test_robots_equal_length_prefers_allow() -> None:
    """同长度时 ``Allow`` 优先。"""
    text = """
User-agent: *
Disallow: /path
Allow: /path
"""
    rules = RobotsRules.parse(text, "rag-ingest-demo/0.1")

    assert rules.allowed("https://example.com/path") is True


async def test_rate_limiter_runs_with_zero_delay() -> None:
    """``delay=0`` 时并发槽正常工作，不会死锁。"""
    limiter = HostRateLimiter(concurrency=2, delay=0)
    entered: list[int] = []

    async def worker(index: int) -> None:
        async with limiter.slot("https://example.com/zh/a"):
            entered.append(index)
            await asyncio.sleep(0)

    await asyncio.gather(*(worker(index) for index in range(4)))

    assert sorted(entered) == [0, 1, 2, 3]


async def test_rate_limiter_spaces_request_starts(sleeps: list[float]) -> None:
    """同一 host 连续两次请求的起始时间至少间隔 ``delay`` 。"""
    limiter = HostRateLimiter(concurrency=1, delay=1.0)

    async with limiter.slot("https://example.com/zh/a"):
        pass
    async with limiter.slot("https://example.com/zh/a"):
        pass

    assert len(sleeps) == 1
    assert 0.9 <= sleeps[0] <= 1.1


async def test_rate_limiter_tracks_hosts_separately(sleeps: list[float]) -> None:
    """不同 host 互不影响，不产生等待。"""
    limiter = HostRateLimiter(concurrency=1, delay=1.0)

    async with limiter.slot("https://a.example.com/zh/"):
        pass
    async with limiter.slot("https://b.example.com/zh/"):
        pass

    assert sleeps == []
