"""
推送层单测：用 ``httpx.MockTransport`` 假造接入 API，不访问真实网络。

**等待用 monkeypatch 掉：** ``crawler.push._sleep`` 换成只记录秒数的协程，
重试用例既能断言「确实退避了」，又不会真的睡几秒。
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

import crawler.push as push
from crawler.config import CrawlConfig
from crawler.extract import ExtractedPage
from crawler.state import CrawlState


@pytest.fixture
def cfg() -> CrawlConfig:
    """固定与推送相关的配置，别受测试容器环境变量影响。"""
    return replace(
        CrawlConfig.from_env(),
        api_base="http://api:8000",
        bulk_size=2,
        max_retries=3,
    )


@pytest.fixture
def state(redis) -> CrawlState:
    """用测试 redis（db 15）构造状态对象；推送层并不写它，只为满足签名。"""
    return CrawlState(redis)


def make_page(**overrides) -> ExtractedPage:
    """
    造一个 ``ExtractedPage`` 。

    :param overrides: 覆盖默认字段（ ``title`` / ``content`` / ``author`` /
        ``updated_at`` / ``canonical_url`` ）。
    :return: 抽取结果。
    """
    fields = {
        "title": "依赖项 - FastAPI",
        "content": "正文内容。" * 20,
        "author": None,
        "updated_at": None,
        "canonical_url": None,
    }
    fields.update(overrides)
    return ExtractedPage(**fields)  # type: ignore[arg-type]


def _bulk_response(count: int, *, created: int | None = None) -> httpx.Response:
    """
    构造一个正常的 ``BulkResult`` 响应。

    :param count: 本批篇数。
    :param created: 新建篇数；默认全部新建。
    :return: HTTP 200 响应。
    """
    return httpx.Response(
        200,
        json={
            "received": count,
            "created": count if created is None else created,
            "updated": 0,
            "duplicates": 0,
            "rejected": 0,
            "items": [],
        },
    )


# ---------- build_payload ----------


def test_build_payload_fields_and_truncation():
    """字段名与接口一致；标题 500、作者 120 截断；时间转 UTC ISO。"""
    updated = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
    page = make_page(title="题" * 600, author="作" * 200, updated_at=updated)

    payload = push.build_payload(
        url="https://fastapi.tiangolo.com/zh/tutorial/dependencies/",
        external_id="deps",
        page=page,
    )

    assert payload is not None
    assert payload["source"] == "web"
    assert payload["external_id"] == "deps"
    assert payload["url"] == "https://fastapi.tiangolo.com/zh/tutorial/dependencies/"
    assert payload["content"] == page.content
    assert len(payload["title"]) == push.MAX_TITLE_CHARS
    assert len(payload["author"]) == push.MAX_AUTHOR_CHARS
    assert datetime.fromisoformat(payload["doc_updated_at"]) == updated


def test_build_payload_empty_optionals():
    """没有作者 / 时间时给 ``None`` ；标题为空退回 URL（避免撞接口的非空校验）。"""
    payload = push.build_payload(
        url="https://example.com/zh/doc/",
        external_id="doc",
        page=make_page(title="", author=""),
    )

    assert payload is not None
    assert payload["author"] is None
    assert payload["doc_updated_at"] is None
    assert payload["title"] == "https://example.com/zh/doc/"


def test_build_payload_skips_overlong_url():
    """URL 超过 500 字符返回 ``None`` （调用方跳过该页，不让整轮失败）。"""
    url = "https://example.com/zh/" + "a" * 500
    assert len(url) > push.MAX_URL_CHARS
    assert push.build_payload(url=url, external_id="x", page=make_page()) is None


# ---------- push_documents ----------


async def test_push_documents_batches_by_bulk_size(cfg, state):
    """5 篇、每批 2 篇 → 三次请求，批量为 ``[2, 2, 1]`` ；计数累计正确。"""
    batches: list[list[dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == push.BULK_PATH
        documents = json.loads(request.content)["documents"]
        batches.append(documents)
        return _bulk_response(len(documents), created=len(documents))

    payloads = [
        push.build_payload(
            url=f"https://example.com/zh/{i}/",
            external_id=f"id-{i}",
            page=make_page(),
        )
        for i in range(5)
    ]

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await push.push_documents(client, payloads, cfg=cfg, state=state)

    assert [len(batch) for batch in batches] == [2, 2, 1]
    assert result["received"] == 5
    assert result["created"] == 5
    assert result["failed"] == 0


async def test_push_documents_empty_input_makes_no_request(cfg, state):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不该被调用
        nonlocal calls
        calls += 1
        return _bulk_response(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await push.push_documents(client, [], cfg=cfg, state=state)

    assert calls == 0
    assert result == {
        "received": 0,
        "created": 0,
        "updated": 0,
        "duplicates": 0,
        "rejected": 0,
        "failed": 0,
    }


async def test_push_documents_retries_5xx_then_succeeds(cfg, state, monkeypatch):
    """首个 503 后退避重试，第二次 200 → 计数正常、``failed`` 为 0。"""
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(push, "_sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="busy")
        return _bulk_response(1)

    payload = push.build_payload(
        url="https://example.com/zh/a/", external_id="a", page=make_page()
    )
    assert payload is not None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await push.push_documents(client, [payload], cfg=cfg, state=state)

    assert calls == 2
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 1.5          # 1s 基数 + 抖动
    assert result["created"] == 1
    assert result["failed"] == 0


async def test_push_documents_exhausts_retries_and_counts_failed(cfg, state, monkeypatch):
    """``max_retries=2`` → 最多尝试 3 次；全 5xx 则该批计入 ``failed`` 。"""
    cfg = replace(cfg, max_retries=2)
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(push, "_sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    payload = push.build_payload(
        url="https://example.com/zh/b/", external_id="b", page=make_page()
    )
    assert payload is not None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await push.push_documents(client, [payload], cfg=cfg, state=state)

    assert calls == 3
    assert len(sleeps) == 2
    assert result["failed"] == 1
    assert result["created"] == 0


async def test_push_documents_429_honours_retry_after(cfg, state, monkeypatch):
    """429 优先用 ``Retry-After`` ，而不是自己的指数退避。"""
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(push, "_sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        return _bulk_response(1)

    payload = push.build_payload(
        url="https://example.com/zh/c/", external_id="c", page=make_page()
    )
    assert payload is not None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await push.push_documents(client, [payload], cfg=cfg, state=state)

    assert calls == 2
    assert sleeps == [7.0]
    assert result["created"] == 1


async def test_push_documents_does_not_retry_4xx(cfg, state, monkeypatch):
    """422 是请求本身的问题：只打一次，直接计失败。"""
    calls = 0
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(push, "_sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(422, text="invalid")

    payload = push.build_payload(
        url="https://example.com/zh/d/", external_id="d", page=make_page()
    )
    assert payload is not None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await push.push_documents(client, [payload], cfg=cfg, state=state)

    assert calls == 1
    assert sleeps == []
    assert result["failed"] == 1


async def test_push_documents_accumulates_rejected(cfg, state):
    """``BulkResult`` 的各计数原样累计，包括 ``duplicates`` / ``rejected`` 。"""

    def handler(request: httpx.Request) -> httpx.Response:
        documents = json.loads(request.content)["documents"]
        return httpx.Response(
            200,
            json={
                "received": len(documents),
                "created": 0,
                "updated": 0,
                "duplicates": len(documents),
                "rejected": 0,
                "items": [],
            },
        )

    payload = push.build_payload(
        url="https://example.com/zh/e/", external_id="e", page=make_page()
    )
    assert payload is not None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await push.push_documents(client, [payload], cfg=cfg, state=state)

    assert result["duplicates"] == 1
    assert result["created"] == 0


# ---------- send_delete ----------


async def test_send_delete_is_idempotent(cfg, state):
    """删除信号重复发送：接口幂等，第一次 ``True`` 、第二次 ``False`` 。"""
    bodies: list[dict] = []
    answers = [True, False]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == push.DELETE_PATH
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"deleted": answers.pop(0)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await push.send_delete(
            client, cfg=cfg, state=state, external_id="https://example.com/zh/gone/"
        )
        second = await push.send_delete(
            client, cfg=cfg, state=state, external_id="https://example.com/zh/gone/"
        )

    assert first is True
    assert second is False
    assert bodies == [
        {"source": "web", "external_id": "https://example.com/zh/gone/"},
        {"source": "web", "external_id": "https://example.com/zh/gone/"},
    ]


async def test_send_delete_returns_none_after_retries(cfg, state, monkeypatch):
    """删除接口一直 5xx → 返回 ``None`` （调用方不要 ``forget`` ，下一轮再试）。"""
    cfg = replace(cfg, max_retries=1)
    calls = 0

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(push, "_sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, text="bad gateway")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deleted = await push.send_delete(
            client, cfg=cfg, state=state, external_id="https://example.com/zh/x/"
        )

    assert calls == 2
    assert deleted is None


async def test_send_delete_returns_none_on_non_json(cfg, state):
    """200 但不是 JSON（例如网关错误页）同样视为请求失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deleted = await push.send_delete(
            client, cfg=cfg, state=state, external_id="https://example.com/zh/y/"
        )

    assert deleted is None
