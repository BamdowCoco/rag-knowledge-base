"""Hybrid 检索（真 MySQL FULLTEXT ngram + 真 Qdrant + ASGI）：关键词召回、过滤、降级。

``hash`` provider 不懂语义，所以这里只验证「关键词一路确实参与了融合」这类结构性行为，
不评价语义相关性。关键词索引缺失 / SQL 失败时必须退回 dense，不能让检索 503。
"""
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.infra.qdrant_client import qdrant_client
from app.models.document_chunk import DocumentChunk
from app.repositories.chunk_repo import ChunkRepo
from app.services.embedding_providers.hash import HashProvider
from embedder.main import index_once

SEARCH = "/api/v1/search"


async def _index(client, limit: int = 50) -> dict[str, int]:
    """
    跑一轮向量化，把 pending chunk 写进 Qdrant。

    :param client: ASGI 测试客户端。
    :param limit: 本轮认领上限。
    :return: ``index_once`` 的计数。
    """
    return await index_once(client, qdrant_client, HashProvider(), limit=limit)


async def _chunk_ids_of(document_id: int) -> list[int]:
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


async def test_hybrid_search_includes_keyword_hits(client, make_doc):
    """hybrid 下关键词一路要真正命中中文术语，并体现在 ``keyword_hits`` 上。"""
    doc = make_doc(
        source="feishu",
        external_id="hybrid-1",
        title="依赖注入指南",
        content="FastAPI 的依赖注入使用 Depends 声明，支持嵌套依赖。",
    )
    document_id = (await client.post("/api/v1/documents", json=doc)).json()["id"]
    await _index(client)

    # 先直接验证仓储层：MATCH ... AGAINST (ngram) 能召回，分数为正
    async with AsyncSessionLocal() as db:
        pairs = await ChunkRepo(db).keyword_search(
            "依赖注入", 50, embedding_model=HashProvider().model
        )
    assert pairs, "FULLTEXT ngram 索引应能命中「依赖注入」"
    assert pairs[0][1] > 0

    r = await client.post(
        SEARCH,
        json={"query": "依赖注入", "top_k": 5, "retrieval_mode": "hybrid"},
    )
    assert r.status_code == 200
    body = r.json()

    assert body["retrieval_mode"] == "hybrid"
    assert body["score_type"] == "rrf"
    assert body["keyword_hits"] >= 1
    # RRF 分是「排名倒数之和」，量级远小于余弦分
    assert all(0 < item["score"] <= 2 for item in body["results"])

    returned_ids = {item["chunk_id"] for item in body["results"]}
    assert returned_ids & set(await _chunk_ids_of(document_id))


async def test_hybrid_search_respects_source_filter(client, make_doc):
    """``filters.source`` 同时作用于 dense 与关键词一路。"""
    target = make_doc(
        source="feishu",
        external_id="hybrid-2a",
        content="依赖注入通过 Depends 声明，feishu 版本。",
    )
    other = make_doc(
        source="yuque",
        external_id="hybrid-2b",
        content="依赖注入通过 Depends 声明，yuque 版本。",
    )
    target_id = (await client.post("/api/v1/documents", json=target)).json()["id"]
    other_id = (await client.post("/api/v1/documents", json=other)).json()["id"]
    await _index(client)

    r = await client.post(
        SEARCH,
        json={
            "query": "依赖注入",
            "top_k": 10,
            "retrieval_mode": "hybrid",
            "filters": {"source": ["feishu"]},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["results"]
    assert all(item["source"] == "feishu" for item in body["results"])
    assert {item["document_id"] for item in body["results"]} == {target_id}
    assert other_id not in {item["document_id"] for item in body["results"]}


async def test_hybrid_falls_back_to_dense_when_keyword_fails(
    client, make_doc, monkeypatch
):
    """关键词索引缺失等异常只记 warning：返回 200，``retrieval_mode`` 退回 dense。"""
    doc = make_doc(
        source="feishu",
        external_id="hybrid-3",
        content="部署手册第一步：安装依赖。第二步：启动服务。",
    )
    document_id = (await client.post("/api/v1/documents", json=doc)).json()["id"]
    await _index(client)

    async def _boom(self, *args, **kwargs):
        """
        模拟 ``MATCH ... AGAINST`` 因索引缺失失败。

        :raises RuntimeError: 固定失败。
        """
        raise RuntimeError("Can't find FULLTEXT index matching the column list")

    monkeypatch.setattr(ChunkRepo, "keyword_search", _boom)

    r = await client.post(
        SEARCH,
        json={"query": "部署手册", "top_k": 5, "retrieval_mode": "hybrid"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["retrieval_mode"] == "dense"
    assert body["score_type"] == "cosine"
    assert body["keyword_hits"] == 0
    assert body["results"]
    assert {item["document_id"] for item in body["results"]} == {document_id}


async def test_default_mode_is_dense(client, make_doc):
    """不传 ``retrieval_mode`` 时走 dense，响应字段保持向后兼容的默认值。"""
    doc = make_doc(source="feishu", external_id="hybrid-4")
    await client.post("/api/v1/documents", json=doc)
    await _index(client)

    r = await client.post(SEARCH, json={"query": "任意问题", "top_k": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["retrieval_mode"] == "dense"
    assert body["score_type"] == "cosine"
    assert body["keyword_hits"] == 0
