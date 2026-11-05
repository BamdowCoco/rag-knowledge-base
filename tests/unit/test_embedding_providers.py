"""embedding / LLM provider 的纯单元测试（不出网）。

OpenAI 兼容协议用 ``httpx.MockTransport`` 假造上游：既能断言请求形状，也能构造
「乱序返回」「维度不对」「坏 JSON」这些真实上游会犯的错，而完全不需要网络。
"""
import json
import math

import httpx
import pytest

import app.infra.ai_client as ai_client
from app.core.config import settings
from app.services.embedding_providers import get_provider, reset_provider_cache
from app.services.embedding_providers.base import EmbeddingError
from app.services.embedding_providers.hash import HashProvider
from app.services.embedding_providers.openai_compat import (
    OpenAICompatEmbeddingProvider,
)
from app.services.llm_providers import reset_provider_cache as reset_llm_provider_cache
from app.services.llm_providers.base import LLMError
from app.services.llm_providers.openai_compat import OpenAICompatLLMProvider


@pytest.fixture(autouse=True)
def _reset_providers():
    """每个测试前后清注册表缓存：测试会改 settings，别让实例带着旧配置串到下一个用例。"""
    reset_provider_cache()
    reset_llm_provider_cache()
    yield
    reset_provider_cache()
    reset_llm_provider_cache()


# ---------- hash ----------


async def test_hash_is_deterministic_and_unit_length():
    """同文本同向量、不同文本不同向量、维度正确、L2 范数为 1。"""
    provider = HashProvider()

    first = await provider.embed(["部署手册第一步"])
    again = await provider.embed(["部署手册第一步"])
    other = await provider.embed(["完全不同的另一段正文"])

    assert first == again
    assert first[0] != other[0]
    assert len(first[0]) == settings.embedding_dim
    norm = math.sqrt(sum(value * value for value in first[0]))
    assert math.isclose(norm, 1.0, rel_tol=1e-6, abs_tol=1e-9)


async def test_hash_empty_input_returns_empty():
    assert await HashProvider().embed([]) == []


def test_reset_provider_cache_rebuilds_instance():
    """清缓存后 ``get_provider`` 必须返回新实例（否则改过的 settings 不生效）。"""
    first = get_provider("hash")
    assert get_provider("hash") is first          # 缓存命中：同一个实例

    reset_provider_cache()
    assert get_provider("hash") is not first


# ---------- openai_compat embedding ----------


async def _fake_embedding_client(handler, monkeypatch):
    """
    把 provider 用的共享 embedding 客户端换成 MockTransport 客户端并返回它。

    provider 内部走 ``get_embedding_client()`` 读模块全局，所以 monkeypatch 模块属性即可。

    :param handler: ``httpx.MockTransport`` 的请求处理函数。
    :param monkeypatch: pytest 的 monkeypatch fixture。
    :return: 假客户端（测试结束时由 ``async with`` 关闭）。
    """
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ai_client, "embedding_client", client)
    return client


async def test_openai_embedding_request_shape_and_index_order(monkeypatch):
    """请求 URL / payload / Bearer 正确；返回乱序时按 ``index`` 与输入对齐。"""
    seen: dict = {}
    dim = settings.embedding_dim

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["authorization"] = request.headers.get("authorization")
        # 故意把 index=1 放在前面：协议不保证返回顺序
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0] * dim},
                    {"index": 0, "embedding": [1.0] * dim},
                ]
            },
        )

    monkeypatch.setattr(settings, "embedding_api_base_url", "http://embedding.test")
    monkeypatch.setattr(settings, "embedding_api_path", "/v1/embeddings")
    monkeypatch.setattr(settings, "embedding_api_key", "secret-key")
    monkeypatch.setattr(settings, "embedding_model", "test-model")

    mock_client = await _fake_embedding_client(handler, monkeypatch)
    async with mock_client:
        provider = OpenAICompatEmbeddingProvider()
        vectors = await provider.embed(["第一段", "第二段"])

    assert vectors[0] == [1.0] * dim              # index=0 → 第一段
    assert vectors[1] == [0.0] * dim              # index=1 → 第二段
    assert seen["url"] == "http://embedding.test/v1/embeddings"
    assert seen["authorization"] == "Bearer secret-key"
    assert seen["body"]["model"] == "test-model"
    assert seen["body"]["input"] == ["第一段", "第二段"]


async def test_openai_embedding_rejects_wrong_dimension(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.0] * (settings.embedding_dim + 1)}]},
        )

    monkeypatch.setattr(settings, "embedding_api_base_url", "http://embedding.test")
    mock_client = await _fake_embedding_client(handler, monkeypatch)
    async with mock_client:
        with pytest.raises(EmbeddingError):
            await OpenAICompatEmbeddingProvider().embed(["一段文本"])


async def test_openai_embedding_rejects_wrong_count(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.0] * settings.embedding_dim}]}
        )

    monkeypatch.setattr(settings, "embedding_api_base_url", "http://embedding.test")
    mock_client = await _fake_embedding_client(handler, monkeypatch)
    async with mock_client:
        with pytest.raises(EmbeddingError):
            await OpenAICompatEmbeddingProvider().embed(["一段", "两段"])


async def test_openai_embedding_requires_base_url(monkeypatch):
    """基址没配是部署错误：立刻抛 ``EmbeddingError``，不要等 HTTP 超时。"""
    monkeypatch.setattr(settings, "embedding_api_base_url", "")
    with pytest.raises(EmbeddingError):
        await OpenAICompatEmbeddingProvider().embed(["一段文本"])


# ---------- openai_compat LLM ----------

_SSE_TEXT = (
    'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
    "data: [DONE]\n\n"
)


async def test_openai_llm_stream_parses_sse_deltas(monkeypatch):
    """SSE 里只取 ``delta.content``，拼出的增量要按顺序、无空串。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            text=_SSE_TEXT,
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(settings, "llm_base_url", "http://llm.test/v1")
    monkeypatch.setattr(settings, "llm_path", "/chat/completions")
    monkeypatch.setattr(settings, "llm_api_key", "")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ai_client, "llm_client", mock_client)
    async with mock_client:
        provider = OpenAICompatLLMProvider()
        pieces = [
            piece
            async for piece in provider.stream(
                question="问题",
                hits=[],
                messages=[{"role": "user", "content": "问题"}],
                max_tokens=16,
                temperature=0.0,
            )
        ]

    assert pieces == ["你", "好"]
    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["body"]["stream"] is True


async def test_openai_llm_bad_json_raises(monkeypatch):
    """流里出现非法 JSON 必须报错，而不是悄悄少一段回答。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="data: {not-json}\n\n", headers={"content-type": "text/event-stream"}
        )

    monkeypatch.setattr(settings, "llm_base_url", "http://llm.test/v1")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(ai_client, "llm_client", mock_client)
    async with mock_client:
        provider = OpenAICompatLLMProvider()
        with pytest.raises(LLMError):
            async for _ in provider.stream(
                question="问题",
                hits=[],
                messages=[{"role": "user", "content": "问题"}],
                max_tokens=16,
                temperature=0.0,
            ):
                pass
