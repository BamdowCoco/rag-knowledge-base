"""统计：聚合 + **chunk 状态分布** （pending 那部分就是「向量化积压」）。"""


async def test_stats_counts_and_chunk_status(client, make_doc):
    await client.post("/api/v1/documents", json=make_doc(source="feishu"))
    await client.post("/api/v1/documents", json=make_doc(source="yuque"))

    body = (await client.get("/api/v1/documents/stats")).json()

    assert body["total"] == 2
    # fetched_at 就是"现在"，换算到北京时间必落在"今天"区间内，不会因跨零点而 flaky
    assert body["today_count"] == 2
    assert {r["source"]: r["count"] for r in body["by_source"]} == {"feishu": 1, "yuque": 1}

    status = {r["status"]: r["count"] for r in body["chunk_status"]}
    assert status.get("pending", 0) >= 2        # 刚入库的块都待向量化
    assert status.get("done", 0) == 0


async def test_stats_excludes_bench_by_default(client, make_doc):
    """bench 压测数据不混进 by_source，而是单独放在 bench_count。"""
    await client.post("/api/v1/documents", json=make_doc(source="web"))
    await client.post("/api/v1/documents", json=make_doc(source="bench"))

    body = (await client.get("/api/v1/documents/stats")).json()

    assert body["total"] == 2
    assert {r["source"]: r["count"] for r in body["by_source"]} == {"web": 1}
    assert body["bench_count"] == 1


async def test_stats_dedup_rejected(client, make_doc):
    doc = make_doc()
    await client.post("/api/v1/documents", json=doc)
    await client.post("/api/v1/documents", json=doc)       # 重复 → 计入 rejected 计数

    body = (await client.get("/api/v1/documents/stats")).json()
    assert body["total"] == 1
    assert body["dedup_rejected"] == 1


async def test_chunk_status_reflects_completion(client, make_doc):
    """worker 把块做完之后，看板上的积压应清零。"""
    await client.post("/api/v1/documents", json=make_doc())

    chunk = (await client.post("/api/v1/chunks/claim", json={"limit": 10})).json()["chunks"][0]
    await client.post(f"/api/v1/chunks/{chunk['id']}/complete")

    body = (await client.get("/api/v1/documents/stats")).json()
    assert {r["status"]: r["count"] for r in body["chunk_status"]} == {"done": 1}
