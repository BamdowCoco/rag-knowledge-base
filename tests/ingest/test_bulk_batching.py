"""批量提交：切片语义必须与逐篇完全一致（含评审指出的几个静默回归点）。"""
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.document_chunk import DocumentChunk
from app.models.document_clean import DocumentClean
from app.models.document_source import DocumentSource
from app.schemas.document import BulkItemResult
from app.services.dedup_service import DEDUP_KEY, DEDUP_REJECTED_KEY, get_rejected_count
from app.services.document_service import DocumentService


async def _active_version(source: str, external_id: str) -> DocumentClean:
    async with AsyncSessionLocal() as db:
        src = (
            await db.execute(
                select(DocumentSource).where(
                    DocumentSource.source == source,
                    DocumentSource.external_id == external_id,
                )
            )
        ).scalar_one()
        return (
            await db.execute(
                select(DocumentClean).where(
                    DocumentClean.source_id == src.id,
                    DocumentClean.superseded_at.is_(None),
                )
            )
        ).scalar_one()


async def test_batch_k1_and_k10_have_same_counts(client, make_doc, monkeypatch):
    """同一份代码，K=1 与 K=10 都应把 12 篇全部新建、id 各不相同。"""
    for k in (1, 10):
        monkeypatch.setattr(settings, "bulk_commit_size", k)
        docs = [make_doc() for _ in range(12)]
        body = (await client.post("/api/v1/documents/bulk", json={"documents": docs})).json()
        assert (body["created"], body["updated"], body["duplicates"], body["rejected"]) == (
            12, 0, 0, 0
        ), f"K={k}"
        assert len({it["id"] for it in body["items"]}) == 12


async def test_same_batch_duplicate_returns_first_id(client, make_doc, monkeypatch):
    monkeypatch.setattr(settings, "bulk_commit_size", 10)
    a = make_doc()
    b = make_doc()

    body = (await client.post("/api/v1/documents/bulk", json={"documents": [a, a, b]})).json()

    assert (body["created"], body["duplicates"], body["rejected"]) == (2, 1, 0)
    assert body["items"][1]["id"] == body["items"][0]["id"]


async def test_same_identity_a_b_a_in_one_slice(client, make_doc, monkeypatch):
    """
    回归：片内 A→B→A 不能被片前的 docver 缓存误判成 duplicate。

    逐篇语义下两篇都是 update（改 B、再改回 A）；批量必须一致，
    最终 active 内容回到 A。
    """
    monkeypatch.setattr(settings, "bulk_commit_size", 10)
    doc = make_doc(source="feishu", external_id="aba-doc", title="标题", content="内容A")
    assert (await client.post("/api/v1/documents", json=doc)).status_code == 201

    body = (
        await client.post(
            "/api/v1/documents/bulk",
            json={"documents": [{**doc, "content": "内容B"}, {**doc, "content": "内容A"}]},
        )
    ).json()

    assert [it["status"] for it in body["items"]] == ["updated", "updated"], body
    assert body["updated"] == 2

    active = await _active_version("feishu", "aba-doc")
    assert active.content == "内容A"

    # 只有最终版本的块是 active
    async with AsyncSessionLocal() as db:
        chunks = (
            await db.execute(select(DocumentChunk).order_by(DocumentChunk.id))
        ).scalars().all()
    assert chunks
    assert all(c.superseded_at is None for c in chunks if c.document_id == active.id)
    assert all(c.superseded_at is not None for c in chunks if c.document_id != active.id)


async def test_cross_identity_same_content_in_one_slice(client, make_doc, monkeypatch):
    """不同身份、相同标题+正文 → 一篇 created、一篇 duplicate（不靠未 flush 的 dedup 行）。"""
    monkeypatch.setattr(settings, "bulk_commit_size", 10)
    a = make_doc(source="feishu", external_id="same-1", title="同一标题", content="同一份正文")
    b = make_doc(source="yuque", external_id="same-2", title="同一标题", content="同一份正文")

    body = (await client.post("/api/v1/documents/bulk", json={"documents": [a, b]})).json()

    assert (body["created"], body["duplicates"], body["rejected"]) == (1, 1, 0), body
    assert body["items"][1]["status"] == "duplicate"
    assert body["items"][1]["id"] == body["items"][0]["id"]


async def test_slice_failure_falls_back_to_per_item(client, make_doc, monkeypatch):
    """片内一次异常 → 整片回退逐篇重放，后面的片不受影响。"""
    monkeypatch.setattr(settings, "bulk_commit_size", 3)
    # 这条测的是 Stage 2 的逐篇切片路径，先关掉多行快路径
    monkeypatch.setattr(settings, "bulk_multirow_enabled", False)
    docs = [make_doc() for _ in range(4)]
    target = docs[1]["external_id"]

    original = DocumentService._apply_identified_in_tx
    calls = {"n": 0}

    async def flaky(self, raw, cleaned, **kwargs):
        if raw["external_id"] == target and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("slice boom")
        return await original(self, raw, cleaned, **kwargs)

    monkeypatch.setattr(DocumentService, "_apply_identified_in_tx", flaky)

    body = (await client.post("/api/v1/documents/bulk", json={"documents": docs})).json()

    assert body["created"] == 4, body
    assert all(it["status"] == "created" for it in body["items"])


async def test_slice_failure_releases_new_marks(client, make_doc, redis, monkeypatch):
    """片回滚时只释放本片 SADD 成功的占位。"""
    monkeypatch.setattr(settings, "bulk_multirow_enabled", False)
    fake_hash = "f" * 64
    original = DocumentService._apply_identified_in_tx

    async def flaky(self, raw, cleaned, **kwargs):
        if raw["external_id"] == "compensate-me":
            kwargs["marks_added"].add(fake_hash)
            await redis.sadd(DEDUP_KEY, fake_hash)
            raise RuntimeError("slice boom")
        return await original(self, raw, cleaned, **kwargs)

    async def fake_replay(self, chunk):
        return {
            i: BulkItemResult(index=i, status="rejected") for i, _, _ in chunk
        }

    monkeypatch.setattr(DocumentService, "_apply_identified_in_tx", flaky)
    monkeypatch.setattr(DocumentService, "_replay_chunk", fake_replay)

    doc = make_doc(external_id="compensate-me")
    body = (await client.post("/api/v1/documents/bulk", json={"documents": [doc]})).json()

    assert body["rejected"] == 1
    assert await redis.sismember(DEDUP_KEY, fake_hash) == 0


async def test_post_commit_failure_keeps_committed_result(client, make_doc, monkeypatch):
    """commit 成功之后 Redis 抛异常 → 不得回滚/重放，响应仍是 created。"""
    async def boom(self, post):
        raise RuntimeError("redis down")

    monkeypatch.setattr(DocumentService, "_apply_post_commit", boom)

    doc = make_doc()
    body = (await client.post("/api/v1/documents/bulk", json={"documents": [doc]})).json()

    assert body["created"] == 1
    listed = (await client.get("/api/v1/documents")).json()
    assert listed["total"] == 1


async def test_counter_counts_each_duplicate_once(client, make_doc, redis, monkeypatch):
    """「重复 + 片失败 + 逐篇重放」组合下，计数器不能双写。"""
    monkeypatch.setattr(settings, "bulk_commit_size", 2)
    monkeypatch.setattr(settings, "bulk_multirow_enabled", False)
    existing = make_doc(content="已经存在的正文")
    assert (await client.post("/api/v1/documents", json=existing)).status_code == 201
    await redis.delete(DEDUP_REJECTED_KEY)

    fresh = make_doc()
    target = fresh["external_id"]
    original = DocumentService._apply_identified_in_tx
    calls = {"n": 0}

    async def flaky(self, raw, cleaned, **kwargs):
        if raw["external_id"] == target and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("slice boom")
        return await original(self, raw, cleaned, **kwargs)

    monkeypatch.setattr(DocumentService, "_apply_identified_in_tx", flaky)

    body = (
        await client.post(
            "/api/v1/documents/bulk", json={"documents": [existing, fresh]}
        )
    ).json()

    assert (body["created"], body["duplicates"]) == (1, 1), body
    assert await get_rejected_count(redis) == 1
