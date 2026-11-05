"""批量摄入：pipeline 判重 + 按条跳过（重复带同一个 id）。"""


async def test_bulk_counts_and_in_batch_duplicate(client, make_doc):
    a = make_doc(title="批量A", content="批量正文AAA。")
    b = make_doc(title="批量B", content="批量正文BBB。")

    r = await client.post("/api/v1/documents/bulk", json={"documents": [a, a, b]})
    assert r.status_code == 200
    body = r.json()

    assert (body["received"], body["created"], body["duplicates"], body["rejected"]) == (3, 2, 1, 0)
    assert body["items"][0]["status"] == "created"
    assert body["items"][1]["status"] == "duplicate"
    assert body["items"][2]["status"] == "created"
    # 同批重复的那条，带的是【第一篇的 id】
    assert body["items"][1]["id"] == body["items"][0]["id"]


async def test_bulk_duplicate_of_existing_document(client, make_doc):
    """批量里的某篇已经在库里 → 记 duplicate，并返回已存在那篇的 id。"""
    doc = make_doc()
    created = (await client.post("/api/v1/documents", json=doc)).json()

    body = (await client.post("/api/v1/documents/bulk", json={"documents": [doc]})).json()
    assert body["created"] == 0
    assert body["duplicates"] == 1
    assert body["items"][0]["id"] == created["id"]


async def test_bulk_rejects_empty_content(client, make_doc):
    r = await client.post(
        "/api/v1/documents/bulk", json={"documents": [make_doc(content="<p></p>")]}
    )
    body = r.json()
    assert (body["received"], body["created"], body["duplicates"], body["rejected"]) == (1, 0, 0, 1)
    assert body["items"][0]["status"] == "rejected"


async def test_bulk_limit_is_capped(client, make_doc):
    """一次最多 200 篇（超了直接 422，不进管道）。"""
    too_many = [make_doc() for _ in range(201)]
    r = await client.post("/api/v1/documents/bulk", json={"documents": too_many})
    assert r.status_code == 422
