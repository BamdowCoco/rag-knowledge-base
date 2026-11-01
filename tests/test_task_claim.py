"""任务认领 / 回报（单线程语义；并发语义见 test_skip_locked.py）。

P0 起认领除了正文，还返回 ``claim_token`` 与所属版本元数据（worker 据此直接拼 Qdrant
payload，不必回查 MySQL）；complete / fail 都要回传 token 做 fencing。
"""
from datetime import timedelta

from sqlalchemy import update

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.document_chunk import DocumentChunk
from app.services.reclaimer import reclaim_once
from app.utils.time import utcnow

CLAIM = "/api/v1/chunks/claim"

# 认领接口的任务单元字段集合（P0 契约，改动要同步改这里）
_CLAIM_FIELDS = {
    "id",
    "document_id",
    "chunk_index",
    "content",
    "token_count",
    "claim_token",
    "source",
    "external_id",
    "title",
    "url",
    "language",
    "tags",
    "content_hash",
    "clean_version",
}


async def _claim_one(client) -> dict:
    """
    认领并返回第一块（这些测试都只造一篇短文档 = 一个 chunk）。

    :param client: ASGI 测试客户端。
    :return: 认领到的 chunk 字典。
    """
    body = (await client.post(CLAIM, json={"limit": 50})).json()
    assert body["chunks"], "应该能认领到一块"
    return body["chunks"][0]


async def test_claim_then_complete(client, make_doc):
    """认领返回正文 + 元数据 + token；认领过的块不会再次下发。"""
    doc = make_doc(source="feishu", external_id="claim-1", title="部署手册")
    await client.post("/api/v1/documents", json=doc)

    r = await client.post(CLAIM, json={"limit": 10})
    assert r.status_code == 200
    chunks = r.json()["chunks"]
    assert len(chunks) >= 1

    chunk = chunks[0]
    # worker 拿到的是任务单元：正文 + 所属版本元数据 + fencing token
    assert set(chunk) == _CLAIM_FIELDS
    assert chunk["claim_token"]
    assert chunk["source"] == "feishu"
    assert chunk["external_id"] == "claim-1"
    assert chunk["title"] == "部署手册"
    assert chunk["content_hash"]
    assert chunk["clean_version"] == 1
    assert chunk["content"]

    # 认领过的已置 processing，再认领拿不到
    again = await client.post(CLAIM, json={"limit": 10})
    assert again.json()["chunks"] == []

    cid = chunk["id"]
    done = await client.post(f"/api/v1/chunks/{cid}/complete")
    assert done.status_code == 200
    assert done.json() == {"id": cid, "embedding_status": "done"}


async def test_complete_with_token_is_idempotent(client, make_doc):
    """带 token 的 complete：首次真的更新，重复调用幂等成功，并记下模型 / 维度 / 时刻。"""
    await client.post("/api/v1/documents", json=make_doc())
    chunk = await _claim_one(client)
    body = {
        "claim_token": chunk["claim_token"],
        "embedding_model": "hash",
        "embedding_dim": 64,
    }

    first = await client.post(f"/api/v1/chunks/{chunk['id']}/complete", json=body)
    assert first.status_code == 200
    assert first.json() == {"id": chunk["id"], "embedding_status": "done"}

    # worker 重复回报（响应丢了、重试）：token 仍匹配，按幂等成功处理
    again = await client.post(f"/api/v1/chunks/{chunk['id']}/complete", json=body)
    assert again.status_code == 200
    assert again.json() == {"id": chunk["id"], "embedding_status": "done"}

    async with AsyncSessionLocal() as db:
        obj = await db.get(DocumentChunk, chunk["id"])
        assert obj is not None
        assert obj.embedding_status == "done"
        assert obj.embedding_model == "hash"
        assert obj.embedding_dim == 64
        assert obj.embedded_at is not None


async def test_stale_claim_token_is_409(client, make_doc):
    """慢 worker 在超时回收、别人重领之后回来 → 旧 token 被拒（fencing）。"""
    await client.post("/api/v1/documents", json=make_doc())
    first = await _claim_one(client)
    cid, stale_token = first["id"], first["claim_token"]

    # 模拟「worker 领完就挂了」：把 updated_at 调早，回收会把它放回 pending
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(DocumentChunk)
            .where(DocumentChunk.id == cid)
            .values(
                updated_at=utcnow()
                - timedelta(seconds=settings.reclaim_after_seconds + 60)
            )
        )
        await db.commit()

    assert await reclaim_once() == 1

    # 重新认领 → 新 token；旧 worker 手里的那个就此失效
    second = await _claim_one(client)
    assert second["id"] == cid
    assert second["claim_token"] != stale_token

    stale = await client.post(
        f"/api/v1/chunks/{cid}/complete", json={"claim_token": stale_token}
    )
    assert stale.status_code == 409

    ok = await client.post(
        f"/api/v1/chunks/{cid}/complete", json={"claim_token": second["claim_token"]}
    )
    assert ok.status_code == 200
    assert ok.json()["embedding_status"] == "done"


async def test_fail_backoff_and_poison_pill(client, make_doc, monkeypatch):
    """失败计数逐次 +1；未达上限退回 pending，达上限转 failed 且永不再被认领。"""
    # 退避基数置 0：默认的 5s/10s 会让第二次认领拿不到东西（测试不该等真实时间）
    monkeypatch.setattr(settings, "embed_backoff_seconds", 0)
    await client.post("/api/v1/documents", json=make_doc())

    statuses: list[str] = []
    for attempt in range(1, settings.max_embed_attempts + 1):
        chunk = await _claim_one(client)          # 每轮都是新 token（失败时旧 token 已被清空）
        r = await client.post(
            f"/api/v1/chunks/{chunk['id']}/fail",
            json={"claim_token": chunk["claim_token"], "error": "boom"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["embedding_attempts"] == attempt
        statuses.append(body["embedding_status"])

        # 等真实退避（哪怕 0 秒）会受 MySQL DATETIME 秒级精度影响：把 next_attempt_at
        # 置空，等价于「退避期已过」，测试不必真的等待或依赖时钟。
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(DocumentChunk)
                .where(DocumentChunk.id == chunk["id"])
                .values(next_attempt_at=None)
            )
            await db.commit()

    # 前 N-1 次退回 pending，第 N 次是终态 failed
    assert statuses[:-1] == ["pending"] * (len(statuses) - 1)
    assert statuses[-1] == "failed"

    # 毒丸：达到上限后不再出现在认领队列里
    assert (await client.post(CLAIM, json={"limit": 50})).json()["chunks"] == []


async def test_complete_without_token_keeps_old_behavior(client, make_doc):
    """不带 body 的 complete 仍按旧行为完成 —— 兼容还没升级的调用方。"""
    await client.post("/api/v1/documents", json=make_doc())
    chunk = await _claim_one(client)

    done = await client.post(f"/api/v1/chunks/{chunk['id']}/complete")
    assert done.status_code == 200
    assert done.json() == {"id": chunk["id"], "embedding_status": "done"}


async def test_complete_unknown_id_is_404(client):
    r = await client.post("/api/v1/chunks/999999/complete")
    assert r.status_code == 404


async def test_claim_limit_out_of_range_is_422(client):
    assert (await client.post("/api/v1/chunks/claim", json={"limit": 0})).status_code == 422
    assert (await client.post("/api/v1/chunks/claim", json={"limit": 999})).status_code == 422
