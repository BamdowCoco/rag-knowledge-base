"""撤回流（阶段 F）：作废【已被 embedding 过】的块才会产生撤回任务。

关键判断： **从没被下游认领过的块不需要撤回** —— 下游手里根本没有它。
"""
CLAIM = "/api/v1/chunks/claim"


async def test_retraction_only_for_already_embedded_chunks(client, make_doc):
    first = make_doc(source="feishu", external_id="ret-1", content="旧版正文")
    await client.post("/api/v1/documents", json=first)

    # 让 worker 把第一版的块做完（done）
    claimed = (await client.post(CLAIM, json={"limit": 50})).json()
    assert claimed["chunks"], "应该认领到第一版的块"
    assert claimed["retractions"] == []          # 这时还没有任何东西要撤回
    for c in claimed["chunks"]:
        await client.post(f"/api/v1/chunks/{c['id']}/complete")

    # 更新 → 第一版的块已经 done 过，所以【要】撤回
    await client.post("/api/v1/documents", json={**first, "content": "新版正文"})

    got = (await client.post(CLAIM, json={"limit": 50})).json()
    retraction_ids = {t["id"] for t in got["retractions"]}
    assert retraction_ids, "旧版【已 embedding】的块应该出现在撤回队列里"

    # 确认之后就不该再出现
    for cid in retraction_ids:
        r = await client.post(f"/api/v1/chunks/{cid}/retracted")
        assert r.status_code == 200
        assert r.json() == {"id": cid, "retracted": True}

    again = (await client.post(CLAIM, json={"limit": 50})).json()
    assert {t["id"] for t in again["retractions"]} == set()


async def test_no_retraction_for_chunks_never_claimed(client, make_doc):
    """作废时还停在 ``pending`` 的块 —— 下游从没见过它 → 不该有撤回任务。"""
    first = make_doc(source="feishu", external_id="ret-2", content="旧版正文")
    await client.post("/api/v1/documents", json=first)
    # 故意【不认领】，直接更新

    await client.post("/api/v1/documents", json={**first, "content": "新版正文"})

    got = (await client.post(CLAIM, json={"limit": 50})).json()
    assert got["retractions"] == []
    # 认领到的只会是新版的块（旧版 pending 块已被 claim_pending 过滤掉）
    assert got["chunks"]


async def test_superseded_pending_chunks_leave_the_backlog(client, make_doc):
    """作废的 pending 块不能一直算进「向量化积压」—— 否则那个数字再也降不到 0。"""
    first = make_doc(source="feishu", external_id="ret-3")
    await client.post("/api/v1/documents", json=first)
    await client.post("/api/v1/documents", json={**first, "content": "新版正文"})

    body = (await client.get("/api/v1/documents/stats")).json()
    status = {r["status"]: r["count"] for r in body["chunk_status"]}
    # 只剩新版那批一块是 pending
    assert status == {"pending": 1}
