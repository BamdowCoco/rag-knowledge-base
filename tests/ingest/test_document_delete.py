"""删除信号（阶段 F）： **幂等** ；删掉后文档从列表消失；源系统再推 = 复活。"""


DELETE = "/api/v1/documents/delete"


async def test_delete_hides_document_and_is_idempotent(client, make_doc):
    doc = make_doc(source="feishu", external_id="del-1")
    assert (await client.post("/api/v1/documents", json=doc)).status_code == 201
    assert (await client.get("/api/v1/documents")).json()["total"] == 1

    body = {"source": "feishu", "external_id": "del-1"}
    first = await client.post(DELETE, json=body)
    assert first.status_code == 200
    assert first.json() == {"deleted": True}
    assert (await client.get("/api/v1/documents")).json()["total"] == 0

    # 删除信号会被反复重发 —— 第二次必须是"什么也没发生"，而不是报错
    again = await client.post(DELETE, json=body)
    assert again.status_code == 200
    assert again.json() == {"deleted": False}


async def test_delete_unknown_identity_is_not_404(client):
    r = await client.post(DELETE, json={"source": "feishu", "external_id": "never-existed"})
    assert r.status_code == 200
    assert r.json() == {"deleted": False}


async def test_deleted_document_can_be_resurrected(client, make_doc):
    """源系统删过、又推回来 → 复活（而不是撞 ``UNIQUE(source, external_id)``）。"""
    doc = make_doc(source="feishu", external_id="res-1")

    await client.post("/api/v1/documents", json=doc)
    await client.post(DELETE, json={"source": "feishu", "external_id": "res-1"})
    assert (await client.get("/api/v1/documents")).json()["total"] == 0

    revived = await client.post("/api/v1/documents", json=doc)
    assert revived.status_code == 200                 # 不是新建，是"复活当前版本"
    assert (await client.get("/api/v1/documents")).json()["total"] == 1


async def test_delete_only_affects_that_identity(client, make_doc):
    keep = make_doc(source="feishu", external_id="keep-1")
    drop = make_doc(source="feishu", external_id="drop-1")
    await client.post("/api/v1/documents", json=keep)
    await client.post("/api/v1/documents", json=drop)

    await client.post(DELETE, json={"source": "feishu", "external_id": "drop-1"})

    listed = (await client.get("/api/v1/documents")).json()
    assert listed["total"] == 1
    assert listed["items"][0]["external_id"] == "keep-1"
