"""多行写入：ID 链、差分语义、回退边界。"""
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.document_chunk import DocumentChunk
from app.models.document_clean import DocumentClean
from app.models.document_dedup import DocumentDedup
from app.models.document_raw import DocumentRaw
from app.models.document_source import DocumentSource
from app.repositories.document_repo import DocumentRepo
from app.services.bulk_planner import BulkPlanner
from app.services.cache_service import cache_get, document_version_key


async def _planner_calls(monkeypatch) -> dict:
    calls = {"n": 0}
    original = BulkPlanner.write

    async def counting(self, plan, **kwargs):
        calls["n"] += 1
        return await original(self, plan, **kwargs)

    monkeypatch.setattr(BulkPlanner, "write", counting)
    return calls


async def test_multirow_id_chain(client, make_doc):
    """一批 3 篇新建 → source/raw/clean/chunk/dedup 的外键全部正确。"""
    docs = [make_doc() for _ in range(3)]
    body = (await client.post("/api/v1/documents/bulk", json={"documents": docs})).json()
    assert body["created"] == 3, body

    async with AsyncSessionLocal() as db:
        for doc in docs:
            src = (
                await db.execute(
                    select(DocumentSource).where(
                        DocumentSource.source == doc["source"],
                        DocumentSource.external_id == doc["external_id"],
                    )
                )
            ).scalar_one()
            clean = (
                await db.execute(
                    select(DocumentClean).where(
                        DocumentClean.source_id == src.id,
                        DocumentClean.superseded_at.is_(None),
                    )
                )
            ).scalar_one()
            raw = (
                await db.execute(select(DocumentRaw).where(DocumentRaw.id == clean.raw_id))
            ).scalar_one()
            assert (raw.source, raw.external_id) == (doc["source"], doc["external_id"])
            assert raw.raw_content == doc["content"]

            chunks = (
                await db.execute(
                    select(DocumentChunk).where(DocumentChunk.document_id == clean.id)
                )
            ).scalars().all()
            assert chunks and all(c.embedding_status == "pending" for c in chunks)

            dedup = (
                await db.execute(
                    select(DocumentDedup).where(
                        DocumentDedup.content_hash == clean.content_hash
                    )
                )
            ).scalar_one()
            assert dedup.clean_id == clean.id


async def test_multirow_path_is_used(client, make_doc, monkeypatch):
    calls = await _planner_calls(monkeypatch)
    body = (
        await client.post(
            "/api/v1/documents/bulk", json={"documents": [make_doc() for _ in range(3)]}
        )
    ).json()
    assert body["created"] == 3
    assert calls["n"] == 1


async def test_multirow_duplicate_returns_existing_id(client, make_doc):
    doc = make_doc()
    created = (await client.post("/api/v1/documents", json=doc)).json()

    body = (await client.post("/api/v1/documents/bulk", json={"documents": [doc]})).json()

    assert body["duplicates"] == 1
    assert body["items"][0]["id"] == created["id"]


async def test_multirow_update_supersedes_old_version(client, make_doc):
    doc = make_doc(content="旧内容")
    old = (await client.post("/api/v1/documents", json=doc)).json()

    new_doc = make_doc()
    body = (
        await client.post(
            "/api/v1/documents/bulk",
            json={"documents": [{**doc, "content": "新内容"}, new_doc]},
        )
    ).json()
    assert (body["updated"], body["created"]) == (1, 1), body

    async with AsyncSessionLocal() as db:
        old_row = (
            await db.execute(select(DocumentClean).where(DocumentClean.id == old["id"]))
        ).scalar_one()
        assert old_row.superseded_at is not None

        old_chunks = (
            await db.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == old["id"])
            )
        ).scalars().all()
        assert old_chunks and all(c.superseded_at is not None for c in old_chunks)

        # 旧内容已从准入索引里释放
        old_dedup = (
            await db.execute(
                select(DocumentDedup).where(
                    DocumentDedup.content_hash == old_row.content_hash
                )
            )
        ).scalar_one_or_none()
        assert old_dedup is None


async def test_multirow_falls_back_on_repeated_identity(client, make_doc, monkeypatch):
    calls = await _planner_calls(monkeypatch)
    a = make_doc()
    b = make_doc()

    body = (await client.post("/api/v1/documents/bulk", json={"documents": [a, a, b]})).json()

    assert (body["created"], body["duplicates"]) == (2, 1)
    assert calls["n"] == 0      # 预检不通过，planner 没写


async def test_multirow_falls_back_on_same_content(client, make_doc, monkeypatch):
    calls = await _planner_calls(monkeypatch)
    a = make_doc(source="feishu", external_id="same-1", title="同一标题", content="同一正文")
    b = make_doc(source="yuque", external_id="same-2", title="同一标题", content="同一正文")

    body = (await client.post("/api/v1/documents/bulk", json={"documents": [a, b]})).json()

    assert (body["created"], body["duplicates"]) == (1, 1)
    assert calls["n"] == 0


async def test_multirow_failure_falls_back_to_per_item(client, make_doc, monkeypatch):
    original = DocumentRepo.insert_cleans_bulk
    raised = {"n": 0}

    async def flaky(self, rows):
        if raised["n"] == 0 and len(rows) > 1:
            raised["n"] += 1
            raise IntegrityError("INSERT document_clean", {}, Exception("boom"))
        return await original(self, rows)

    monkeypatch.setattr(DocumentRepo, "insert_cleans_bulk", flaky)

    docs = [make_doc() for _ in range(3)]
    body = (await client.post("/api/v1/documents/bulk", json={"documents": docs})).json()

    assert body["created"] == 3, body
    assert raised["n"] == 1


async def test_multirow_disabled_uses_per_item(client, make_doc, monkeypatch):
    calls = await _planner_calls(monkeypatch)
    monkeypatch.setattr(settings, "bulk_multirow_enabled", False)

    body = (
        await client.post(
            "/api/v1/documents/bulk", json={"documents": [make_doc() for _ in range(3)]}
        )
    ).json()

    assert body["created"] == 3
    assert calls["n"] == 0


async def test_multirow_chunk_row_cap_falls_back(client, make_doc, monkeypatch):
    calls = await _planner_calls(monkeypatch)
    monkeypatch.setattr(settings, "bulk_max_chunk_rows", 1)

    body = (
        await client.post(
            "/api/v1/documents/bulk", json={"documents": [make_doc() for _ in range(2)]}
        )
    ).json()

    assert body["created"] == 2
    assert calls["n"] == 0


async def test_multirow_docver_points_to_final_version(client, make_doc, redis):
    docs = [make_doc() for _ in range(2)]
    body = (await client.post("/api/v1/documents/bulk", json={"documents": docs})).json()
    assert body["created"] == 2

    for doc, item in zip(docs, body["items"]):
        cached = await cache_get(
            redis, document_version_key(doc["source"], doc["external_id"])
        )
        assert cached is not None
        assert cached["clean_id"] == item["id"]


async def test_multirow_content_conflict_falls_back(client, make_doc):
    a = make_doc(source="feishu", external_id="conf-a", content="A 的内容")
    b = make_doc(source="feishu", external_id="conf-b", title="B 的标题", content="B 的内容")
    assert (await client.post("/api/v1/documents", json=a)).status_code == 201
    b_row = (await client.post("/api/v1/documents", json=b)).json()

    # A 改成 B 的标题+正文 → 内容已被 B 持有 → 逐篇路径判冲突
    body = (
        await client.post(
            "/api/v1/documents/bulk",
            json={"documents": [{**a, "title": b["title"], "content": b["content"]}]},
        )
    ).json()
    assert body["rejected"] == 1, body

    listed = (await client.get("/api/v1/documents")).json()
    contents = {row["content"] for row in listed["items"]}
    assert "A 的内容" in contents and "B 的内容" in contents


async def test_multirow_transient_error_retries_whole_slice(client, make_doc, monkeypatch):
    """1213 → rollback → 重新 plan → 重试整片，而不是回退逐篇。"""
    original = BulkPlanner.write
    calls = {"n": 0}

    async def flaky(self, plan, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("INSERT", {}, Exception(1213, "deadlock"))
        return await original(self, plan, **kwargs)

    monkeypatch.setattr(BulkPlanner, "write", flaky)

    docs = [make_doc() for _ in range(3)]
    body = (await client.post("/api/v1/documents/bulk", json={"documents": docs})).json()

    assert body["created"] == 3, body
    assert calls["n"] == 2          # 第一次撞瞬时错误，重新 plan 后第二次成功
