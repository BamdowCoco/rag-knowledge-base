"""更新语义（阶段 F）：同一个 ``external_id`` 内容变了 → **更新** （新版本 + 旧版本作废），而不是新增一篇。"""
import json

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.document_chunk import DocumentChunk
from app.services.cache_service import document_version_key


async def _chunks_of(document_id: int) -> list[DocumentChunk]:
    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.id)
        )
        return list(rows.scalars().all())


async def test_new_content_for_same_identity_updates(client, make_doc):
    first = make_doc(source="feishu", external_id="doc-1", content="第一版正文")
    r1 = await client.post("/api/v1/documents", json=first)
    assert r1.status_code == 201

    r2 = await client.post("/api/v1/documents", json={**first, "content": "第二版正文"})
    assert r2.status_code == 200
    assert r2.json()["id"] != r1.json()["id"]      # 更新 = 新版本 = 新行

    # 列表里只该看到【当前版本】那一行
    listed = (await client.get("/api/v1/documents")).json()
    assert listed["total"] == 1
    assert listed["items"][0]["content"] == "第二版正文"
    assert listed["items"][0]["external_id"] == "doc-1"


async def test_same_content_for_same_identity_skips(client, make_doc):
    doc = make_doc(source="feishu", external_id="doc-2")

    r1 = await client.post("/api/v1/documents", json=doc)
    r2 = await client.post("/api/v1/documents", json=doc)

    assert (r1.status_code, r2.status_code) == (201, 200)
    assert r2.json()["id"] == r1.json()["id"]      # 回的是【当前那一版】，不是新行
    assert (await client.get("/api/v1/documents")).json()["total"] == 1


async def test_update_supersedes_old_version_and_its_chunks(client, make_doc):
    first = make_doc(source="feishu", external_id="doc-3", content="旧版本正文")
    old_id = (await client.post("/api/v1/documents", json=first)).json()["id"]

    new_id = (
        await client.post("/api/v1/documents", json={**first, "content": "新版本正文"})
    ).json()["id"]

    old_chunks = await _chunks_of(old_id)
    new_chunks = await _chunks_of(new_id)
    assert old_chunks and new_chunks
    # 旧版本的块全部作废；新版本的块是待处理
    assert all(c.superseded_at is not None for c in old_chunks)
    assert all(c.superseded_at is None for c in new_chunks)
    assert all(c.embedding_status == "pending" for c in new_chunks)


async def test_same_content_under_a_different_identity_is_rejected_as_duplicate(client, make_doc):
    """
    **跨源转载** ：同一份内容从另一个源推过来 → 被「准入检查」拦下，指向已有那篇。

    这是内容去重（Redis 快路径 + ``document_dedup`` 唯一索引）的正面例子 ——
    内容唯一性与"文档身份"是 **两件事** ：身份由 ``(source, external_id)`` 回答，
    内容由 ``content_hash`` 回答。
    （阶段 F 之前这条走的是"没有身份"那条路，现在挪到了「新建准入」。）
    """
    first = make_doc(source="feishu", external_id="a-1", title="同名标题", content="一模一样的正文")
    second = make_doc(
        source="confluence", external_id="b-1", title="同名标题", content="一模一样的正文"
    )

    r1 = await client.post("/api/v1/documents", json=first)
    assert r1.status_code == 201

    r2 = await client.post("/api/v1/documents", json=second)
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]     # 指向已有那篇，而不是新建一篇
    assert (await client.get("/api/v1/documents")).json()["total"] == 1




async def test_fast_path_cache_written_and_cleared(client, make_doc, redis):
    """
    快路径的依据 —— 身份版本缓存： **写入后 write-through，删除后清掉** 。

    删除必须清缓存，否则下次推同样的内容会命中缓存，把「已删除的版本」直接回给调用方。
    """
    key = document_version_key("feishu", "fp-1")
    doc = make_doc(source="feishu", external_id="fp-1")

    created = (await client.post("/api/v1/documents", json=doc)).json()
    raw = await redis.get(key)
    assert raw is not None
    cached = json.loads(raw)
    assert cached["clean_id"] == created["id"]
    assert cached["content_hash"]

    await client.post(
        "/api/v1/documents/delete", json={"source": "feishu", "external_id": "fp-1"}
    )
    assert await redis.get(key) is None
