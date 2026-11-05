"""真实 embedder 的一轮工作（``index_once``）：写 Qdrant、撤回删点、失败不计点。

用 ``hash`` provider（测试环境默认）+ 真 Qdrant 测试集合；索引路径与生产完全一致，
只是向量来源换成了确定性桩。
"""
from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.infra.qdrant_client import qdrant_client
from app.models.document_chunk import DocumentChunk
from app.services.embedding_providers.hash import HashProvider
from embedder.main import index_once


async def _index(client, provider, limit: int = 50) -> dict[str, int]:
    """
    跑一轮真实的 index_once（认领 → embedding → upsert → complete / retract）。

    :param client: 指向应用的 ASGI 测试客户端。
    :param provider: embedding provider。
    :param limit: 本轮认领上限。
    :return: ``index_once`` 的计数。
    """
    return await index_once(client, qdrant_client, provider, limit=limit)


async def _chunk_ids(document_id: int) -> list[int]:
    """
    取某文档版本下 **未作废** 的 chunk id。

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


async def _points(chunk_ids: list[int]):
    """
    按 id 从测试集合里取 point（带 payload、不带向量）。

    :param chunk_ids: chunk id 列表。
    :return: Qdrant ``Record`` 列表；不存在的 id 会被直接省略。
    """
    if not chunk_ids:
        return []
    return await qdrant_client.retrieve(
        collection_name=settings.qdrant_collection,
        ids=chunk_ids,
        with_payload=True,
        with_vectors=False,
    )


async def test_index_once_writes_payload(client, make_doc):
    """入库 → index_once → 点被写入，payload 带齐检索与展示所需的元数据。"""
    provider = HashProvider()
    doc = make_doc(
        source="feishu",
        external_id="idx-1",
        title="部署手册",
        url="https://example.com/deploy",
    )
    created = await client.post("/api/v1/documents", json=doc)
    assert created.status_code == 201
    document_id = created.json()["id"]

    stats = await _index(client, provider)
    assert stats["done"] >= 1
    assert stats["failed"] == 0
    assert stats["complete_failed"] == 0

    chunk_ids = await _chunk_ids(document_id)
    assert chunk_ids

    records = await _points(chunk_ids)
    by_id = {int(record.id): record.payload for record in records}
    assert set(by_id) == set(chunk_ids)

    payload = by_id[chunk_ids[0]]
    assert payload["chunk_id"] == chunk_ids[0]
    assert payload["document_id"] == document_id
    assert payload["title"] == "部署手册"
    assert payload["source"] == "feishu"
    assert payload["url"] == "https://example.com/deploy"
    assert payload["embedding_model"] == provider.model
    assert payload["embedding_dim"] == provider.dim
    assert payload["content_hash"]
    # 正文只存在 MySQL：payload 里不该再存一份
    assert "content" not in payload

    # MySQL 侧也要记下模型 / 维度 / 完成时刻
    async with AsyncSessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(DocumentChunk).where(DocumentChunk.id.in_(chunk_ids))
                )
            ).scalars()
        )
    assert rows
    for row in rows:
        assert row.embedding_status == "done"
        assert row.embedding_model == provider.model
        assert row.embedding_dim == provider.dim
        assert row.embedded_at is not None


async def test_update_supersedes_and_deletes_old_points(client, make_doc):
    """同一 ``external_id`` 改内容：新版写进去，旧版的点被撤回删除。"""
    provider = HashProvider()
    first = make_doc(source="feishu", external_id="idx-2", content="第一版正文")
    old_document_id = (await client.post("/api/v1/documents", json=first)).json()["id"]

    await _index(client, provider)
    old_chunk_ids = await _chunk_ids(old_document_id)
    assert old_chunk_ids
    assert len(await _points(old_chunk_ids)) == len(old_chunk_ids)   # 旧版先写进去

    updated = await client.post(
        "/api/v1/documents", json={**first, "content": "第二版正文"}
    )
    assert updated.status_code == 200
    new_document_id = updated.json()["id"]
    assert new_document_id != old_document_id

    stats = await _index(client, provider)
    assert stats["done"] >= 1          # 先补齐新版
    assert stats["retracted"] >= 1     # 再撤回旧版

    new_chunk_ids = await _chunk_ids(new_document_id)
    assert new_chunk_ids
    assert len(await _points(new_chunk_ids)) == len(new_chunk_ids)
    # 旧版本的向量已被删除；即便 Qdrant 删慢了，检索侧还会用 MySQL active 过滤兜底
    assert await _points(old_chunk_ids) == []


async def test_delete_removes_points(client, make_doc):
    """删除信号 → index_once 把该文档的向量全部删掉。"""
    provider = HashProvider()
    doc = make_doc(source="feishu", external_id="idx-3")
    document_id = (await client.post("/api/v1/documents", json=doc)).json()["id"]

    await _index(client, provider)
    chunk_ids = await _chunk_ids(document_id)
    assert chunk_ids
    assert len(await _points(chunk_ids)) == len(chunk_ids)

    deleted = await client.post(
        "/api/v1/documents/delete",
        json={"source": doc["source"], "external_id": doc["external_id"]},
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}

    stats = await _index(client, provider)
    assert stats["retracted"] >= 1
    assert await _points(chunk_ids) == []


class _FailingProvider:
    """每次 embed 都失败：验证失败被如实回报、且不会留下半写的点。"""

    name = "hash"
    model = "hash"
    dim = settings.embedding_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        无论输入是什么都抛错。

        :param texts: 文本列表。
        :raises RuntimeError: 固定失败。
        """
        raise RuntimeError("boom")


async def test_embedding_failure_reports_and_retries(client, make_doc):
    """provider 抛错 → 逐块回报 fail（attempts +1，仍可重试），Qdrant 里没有点。"""
    await client.post("/api/v1/documents", json=make_doc(source="feishu", external_id="idx-4"))

    stats = await _index(client, _FailingProvider())
    assert stats["failed"] >= 1
    assert stats["done"] == 0

    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(select(DocumentChunk))).scalars())
    assert rows
    for row in rows:
        assert row.embedding_status == "pending"      # 未达上限 → 退回待重试
        assert row.embedding_attempts == 1
        assert row.embedding_error and "boom" in row.embedding_error

    assert await _points([row.id for row in rows]) == []
