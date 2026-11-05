"""请求级缓存开关：``use_cache=false`` 既不读也不写精确 QA 缓存。

测试环境 ``LLM_PROVIDER=extractive``，不依赖外部模型；``_clean`` 每个测试前会
flush 当前 Redis db，所以缓存状态不会跨测试残留。
"""
import json

from app.core.config import settings
from app.infra.qdrant_client import qdrant_client
from app.services.embedding_providers.hash import HashProvider
from embedder.main import index_once

CHAT = "/api/v1/chat/stream"


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """
    把 SSE 响应文本解析成 ``(事件名, data)`` 列表。

    :param text: 完整响应体。
    :return: 按出现顺序排列的事件列表。
    """
    events: list[tuple[str, dict]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        name: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if name and data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


def _done(events: list[tuple[str, dict]]) -> dict:
    """
    取出唯一的 ``done`` 事件。

    :param events: 解析后的事件列表。
    :return: ``done`` 事件的 data。
    """
    done = [data for name, data in events if name == "done"]
    assert len(done) == 1, f"done 事件应该恰好出现一次，实际 {len(done)} 次"
    return done[0]


async def _prepare(client, make_doc, external_id: str) -> int:
    """
    造一篇短文档并跑完向量化，让检索有东西可召回。

    :param client: ASGI 测试客户端。
    :param make_doc: 唯一内容文档工厂。
    :param external_id: 文档身份。
    :return: 文档 id。
    """
    doc = make_doc(
        source="feishu",
        external_id=external_id,
        title="部署手册",
        content="部署手册第一步：安装依赖。第二步：启动服务。",
    )
    document_id = (await client.post("/api/v1/documents", json=doc)).json()["id"]
    await index_once(client, qdrant_client, HashProvider(), limit=50)
    return document_id


async def _chat(client, question: str, use_cache: bool) -> dict:
    """
    发一次问答请求并返回 ``done`` 事件。

    :param client: ASGI 测试客户端。
    :param question: 用户问题。
    :param use_cache: 请求级缓存开关。
    :return: ``done`` 事件 data。
    """
    r = await client.post(
        CHAT,
        json={"question": question, "top_k": 5, "use_cache": use_cache},
    )
    assert r.status_code == 200
    return _done(parse_sse(r.text))


async def test_use_cache_false_never_reads_or_writes(client, make_doc):
    """连续两次 ``use_cache=false`` 都不命中缓存；之后 ``true`` 也读不到东西。"""
    assert settings.qa_cache_enabled is True
    await _prepare(client, make_doc, "cache-1")
    question = "部署手册第一步是什么？"

    first = await _chat(client, question, use_cache=False)
    second = await _chat(client, question, use_cache=False)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is False

    # 前两次既不读也不写，所以第一次开启缓存的请求仍然是 miss
    warm = await _chat(client, question, use_cache=True)
    assert warm["cache_hit"] is False


async def test_use_cache_true_hits_on_second_request(client, make_doc):
    """``use_cache=true`` 时第一次写缓存、第二次命中。"""
    await _prepare(client, make_doc, "cache-2")
    question = "部署手册第一步是什么？"

    first = await _chat(client, question, use_cache=True)
    assert first["cache_hit"] is False

    second = await _chat(client, question, use_cache=True)
    assert second["cache_hit"] is True
    # 回放时也要带上检索模式等诊断字段（老缓存没有该字段时默认 dense）
    assert second["retrieval_mode"] == "dense"
    assert second["score_type"] == "cosine"
    assert second["latency_ms"] == {"retrieval": 0, "first_token": 0, "total": 0}
