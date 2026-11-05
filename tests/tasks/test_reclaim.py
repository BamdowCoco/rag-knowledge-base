"""超时回收：卡在 ``processing`` 太久的块回到 ``pending``。"""
from datetime import timedelta

from sqlalchemy import update

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.document_chunk import DocumentChunk
from app.repositories.chunk_repo import ChunkRepo
from app.services.reclaimer import reclaim_once
from app.utils.time import utcnow


async def test_reclaim_returns_stuck_chunk_to_pending(client, make_doc):
    """模拟"worker 领完就挂了"：把 updated_at 调早，回收应把它放回 pending。"""
    await client.post("/api/v1/documents", json=make_doc())

    async with AsyncSessionLocal() as db:
        claimed = await ChunkRepo(db).claim_pending(10)      # → processing
        assert len(claimed) == 1
        chunk_id = claimed[0].id

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(DocumentChunk)
            .where(DocumentChunk.id == chunk_id)
            .values(
                updated_at=utcnow()
                - timedelta(seconds=settings.reclaim_after_seconds + 60)
            )
        )
        await db.commit()

    assert await reclaim_once() == 1

    async with AsyncSessionLocal() as db:
        obj = await db.get(DocumentChunk, chunk_id)
        assert obj is not None
        assert obj.embedding_status == "pending"


async def test_reclaim_leaves_fresh_processing_alone(client, make_doc):
    """刚被认领、还"新鲜"的块不该被回收（否则会和正在干活的 worker 抢）。"""
    await client.post("/api/v1/documents", json=make_doc())
    async with AsyncSessionLocal() as db:
        assert len(await ChunkRepo(db).claim_pending(10)) == 1

    assert await reclaim_once() == 0
