"""LLM 用量统计：extractive、缓存命中、真实 LLM token/费用、Redis 降级。"""
import json

from app.api.deps import get_llm_provider
from app.core.config import settings
from app.infra.qdrant_client import qdrant_client
from app.infra.redis_client import get_redis
from app.main import app
from app.services.embedding_providers.hash import HashProvider
from app.services.llm_pricing import estimate_cost
from embedder.main import index_once

CHAT = "/api/v1/chat/stream"
USAGE = "/api/v1/documents/llm-usage"


def _done_event(sse_text: str) -> dict:
    """
    从 SSE 文本里取 ``done`` 事件的数据。

    :param sse_text: 完整 SSE 响应。
    :return: ``done`` 事件的 JSON 对象。
    """
    for block in sse_text.replace("\r\n", "\n").split("\n\n"):
        name = None
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if name == "done" and data_lines:
            return json.loads("\n".join(data_lines))
    raise AssertionError("没有找到 done 事件")


async def _prepare(client, make_doc, external_id: str = "usage-1") -> None:
    """
    造一篇文档并完成向量化，让问答有上下文。

    :param client: ASGI 测试客户端。
    :param make_doc: 文档工厂。
    :param external_id: 文档身份。
    """
    doc = make_doc(
        source="feishu",
        external_id=external_id,
        title="部署手册",
        content="部署手册第一步：安装依赖。第二步：启动服务。",
    )
    await client.post("/api/v1/documents", json=doc)
    await index_once(client, qdrant_client, HashProvider(), limit=50)


class _FakeLLM:
    """固定产出一段带引用的回答，并模拟上游返回 usage。"""

    name = "fake"

    async def stream(self, *, usage_out=None, **kwargs):
        """
        产出固定答案，并写入真实 usage（含 cache hit token）。

        :param usage_out: AnswerService 传入的 usage 容器。
        :yield: 回答文本。
        """
        if usage_out is not None:
            usage_out.update(
                {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                    "prompt_cache_hit_tokens": 4,
                    "prompt_cache_miss_tokens": 16,
                }
            )
        yield "这是模型生成的回答 [1]"


async def test_usage_empty_redis_returns_zeros(client):
    """还没有问答时，今日/累计都是 0，费用为 None。"""
    body = (await client.get(USAGE)).json()

    assert body["total"]["requests"] == 0
    assert body["today"]["requests"] == 0
    assert body["total"]["estimated_cost"] is None


async def test_usage_records_extractive_call(client, make_doc):
    """主动 extractive 计为 extractive_calls，不产生 LLM token。"""
    await _prepare(client, make_doc, "usage-2")
    response = await client.post(
        CHAT, json={"question": "部署手册第一步是什么？", "top_k": 5}
    )

    body = (await client.get(USAGE)).json()
    total = body["total"]

    assert total["requests"] == 1
    assert total["extractive_calls"] == 1
    assert total["llm_calls"] == 0
    assert total["priced_calls"] == 0
    assert total["total_tokens"] == 0
    assert _done_event(response.text)["estimated_cost"] is None


async def test_usage_counts_cache_hit(client, make_doc):
    """同一问题第二次命中精确缓存：requests +1，cache_hits +1，不重复计 token。"""
    await _prepare(client, make_doc, "usage-3")
    payload = {"question": "部署手册第一步是什么？", "top_k": 5}
    await client.post(CHAT, json=payload)
    await client.post(CHAT, json=payload)

    total = (await client.get(USAGE)).json()["total"]

    assert total["requests"] == 2
    assert total["cache_hits"] == 1
    assert total["extractive_calls"] == 1


async def test_usage_records_real_llm_tokens_and_cost(
    client, make_doc, monkeypatch
):
    """真实 LLM 路径使用响应 usage（含 cache token）并按官方价格表计算费用。"""
    await _prepare(client, make_doc, "usage-4")
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    monkeypatch.setattr(settings, "llm_model", "deepseek-flash")
    app.dependency_overrides[get_llm_provider] = lambda: _FakeLLM()
    try:
        response = await client.post(
            CHAT, json={"question": "部署手册第一步是什么？", "top_k": 5}
        )
    finally:
        app.dependency_overrides.pop(get_llm_provider, None)

    body = (await client.get(USAGE)).json()
    total = body["total"]
    expected = estimate_cost("deepseek-flash", 20, 10, cached_tokens=4)

    assert total["llm_calls"] == 1
    assert total["extractive_calls"] == 0
    assert total["prompt_tokens"] == 20
    assert total["completion_tokens"] == 10
    assert total["total_tokens"] == 30
    assert total["priced_calls"] == 1
    assert total["estimated_cost"] == expected
    assert body["currency"] == "CNY"
    assert body["pricing_source"].startswith("https://api-docs.deepseek.com")
    assert body["pricing_period"] in ("peak", "off-peak")
    assert body["price_input_cache_miss_per_1m"] is not None

    done = _done_event(response.text)
    assert done["usage"]["estimated"] is False
    assert done["usage"]["cached_tokens"] == 4
    assert done["estimated_cost"] == expected
    assert done["cost_currency"] == "CNY"


class _BrokenRedis:
    """读取就抛连接的假 Redis —— 模拟运维页遇到 Redis 抖动。"""

    async def hgetall(self, *args, **kwargs):
        """:raise ConnectionError: 永远失败。"""
        raise ConnectionError("redis down")


async def test_usage_degrades_when_redis_unavailable(client):
    """Redis 不可用时接口不 500，返回全 0 窗口。"""

    async def broken_redis():
        yield _BrokenRedis()

    app.dependency_overrides[get_redis] = broken_redis
    try:
        response = await client.get(USAGE)
    finally:
        app.dependency_overrides.pop(get_redis, None)

    assert response.status_code == 200
    assert response.json()["total"]["requests"] == 0
