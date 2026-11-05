"""查询与缓存：分页/过滤、404、分块列表、写后读的新鲜度。"""


async def test_list_pagination_and_source_filter(client, make_doc):
    for i in range(5):
        await client.post(
            "/api/v1/documents", json=make_doc(source="feishu" if i < 3 else "yuque")
        )

    page1 = (await client.get("/api/v1/documents", params={"page": 1, "limit": 2})).json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2

    only_yuque = (await client.get("/api/v1/documents", params={"source": "yuque"})).json()
    assert only_yuque["total"] == 2
    assert {i["source"] for i in only_yuque["items"]} == {"yuque"}


async def test_get_missing_document_is_404(client):
    assert (await client.get("/api/v1/documents/999999")).status_code == 404


async def test_document_chunks_endpoint(client, make_doc):
    doc = (await client.post("/api/v1/documents", json=make_doc())).json()
    r = await client.get(f"/api/v1/documents/{doc['id']}/chunks")
    assert r.status_code == 200
    assert r.json()["total"] >= 1


async def test_chunks_of_missing_document_is_404(client):
    assert (await client.get("/api/v1/documents/999999/chunks")).status_code == 404


async def test_read_after_write_sees_new_data(client, make_doc):
    """
    写入后立刻读列表就能看到新数据 —— 版本号让旧列表缓存 **O(1) 失效** 。

    先垫一次 GET 把缓存捂热，再写一篇；第二次 GET 若还读旧缓存，这里就会是 0。
    """
    assert (await client.get("/api/v1/documents")).json()["total"] == 0

    await client.post("/api/v1/documents", json=make_doc())

    assert (await client.get("/api/v1/documents")).json()["total"] == 1
