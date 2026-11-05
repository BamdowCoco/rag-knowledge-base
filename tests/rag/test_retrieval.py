"""检索接口（``POST /api/v1/search``）：召回候选 + MySQL active 过滤。

``hash`` provider 不懂语义，所以这里 **不测「最相关」** —— 只测候选集范围、
过滤条件生效、以及更新 / 删除后旧内容不再可检索（MySQL 是真相，Qdrant 只是索引）。
"""
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.infra.qdrant_client import qdrant_client
from app.models.document_chunk import DocumentChunk
from app.services.embedding_providers.hash import HashProvider
from embedder.main import index_once

SEARCH = "/api/v1/search"


async def _index(client, limit: int = 50) -> dict[str, int]:
    """
    跑一轮向量化（把待处理 chunk 写进 Qdrant）。

    :param client: ASGI 测试客户端。
    :param limit: 本轮认领上限。
    :return: ``index_once`` 的计数。
    """
    return await index_once(client, qdrant_client, HashProvider(), limit=limit)


async def _active_chunk_ids(document_id: int) -> list[int]:
    """
    取某文档版本下未作废的 chunk id。

    :param document_id: ``document_clean.id``。
    :return: chunk id 列表。
    """
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(DocumentChunk.id).where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.superseded_at.is_(None),
            )
        )
        return list(rows.scalars().all())


async def test_search_returns_indexed_chunks(client, make_doc):
    """入库 + index_once 后能搜到：结果来自该文档、带正文、模型名正确。"""
    doc = make_doc(
        source="feishu",
        external_id="search-1",
        title="部署手册",
        content="部署手册第一步：安装依赖。第二步：启动服务。",
    )
    document_id = (await client.post("/api/v1/documents", json=doc)).json()["id"]
    await _index(client)

    r = await client.post(SEARCH, json={"query": "部署手册第一步", "top_k": 5})
    assert r.status_code == 200
    body = r.json()

    assert body["embedding_model"] == "hash"
    assert body["latency_ms"] >= 0
    assert body["results"]

    active_ids = set(await _active_chunk_ids(document_id))
    returned_ids = {item["chunk_id"] for item in body["results"]}
    assert returned_ids <= active_ids
    for item in body["results"]:
        assert item["document_id"] == document_id
        assert item["content"]
        assert item["title"] == "部署手册"
        assert isinstance(item["score"], float)


async def test_search_filters_by_document_ids(client, make_doc):
    """``filters.document_ids`` 命中本文件；不存在的 id 返回空。"""
    doc = make_doc(source="feishu", external_id="search-2")
    document_id = (await client.post("/api/v1/documents", json=doc)).json()["id"]
    await _index(client)

    hit = await client.post(
        SEARCH,
        json={
            "query": "任意问题",
            "top_k": 5,
            "filters": {"document_ids": [document_id]},
        },
    )
    assert hit.status_code == 200
    assert hit.json()["results"]
    assert all(item["document_id"] == document_id for item in hit.json()["results"])

    miss = await client.post(
        SEARCH,
        json={"query": "任意问题", "top_k": 5, "filters": {"document_ids": [999999]}},
    )
    assert miss.status_code == 200
    assert miss.json()["results"] == []


async def test_search_excludes_superseded_chunks(client, make_doc):
    """同 ``external_id`` 更新后：旧 chunk 不可再检索，只剩新版本的块。"""
    first = make_doc(source="feishu", external_id="search-3", content="旧版部署手册内容")
    old_document_id = (await client.post("/api/v1/documents", json=first)).json()["id"]
    await _index(client)

    old_chunk_ids = set(await _active_chunk_ids(old_document_id))
    assert old_chunk_ids

    updated = await client.post(
        "/api/v1/documents", json={**first, "content": "新版部署手册内容"}
    )
    assert updated.status_code == 200
    new_document_id = updated.json()["id"]
    await _index(client)

    r = await client.post(SEARCH, json={"query": "旧版部署手册内容", "top_k": 50})
    assert r.status_code == 200
    results = r.json()["results"]
    assert results, "新版向量应该可检索到"
    returned_ids = {item["chunk_id"] for item in results}

    assert returned_ids.isdisjoint(old_chunk_ids)
    assert returned_ids <= set(await _active_chunk_ids(new_document_id))


async def test_search_returns_empty_after_delete(client, make_doc):
    """删除文档 + 撤回后，检索不到任何东西。"""
    doc = make_doc(source="feishu", external_id="search-4")
    await client.post("/api/v1/documents", json=doc)
    await _index(client)

    before = await client.post(SEARCH, json={"query": "任意问题", "top_k": 5})
    assert before.json()["results"]

    deleted = await client.post(
        "/api/v1/documents/delete",
        json={"source": doc["source"], "external_id": doc["external_id"]},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}

    await _index(client)

    after = await client.post(SEARCH, json={"query": "任意问题", "top_k": 5})
    assert after.status_code == 200
    assert after.json()["results"] == []
