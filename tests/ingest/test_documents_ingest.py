"""单篇摄入： **重复不是错误** （201 → 200），且判重是「先标准化、后哈希」。"""


async def test_first_201_then_200_same_id(client, make_doc):
    doc = make_doc()

    r1 = await client.post("/api/v1/documents", json=doc)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/documents", json=doc)
    assert r2.status_code == 200

    # 返回的是【已存在那篇】，不是新插一条
    assert r2.json()["id"] == r1.json()["id"]


async def test_normalize_before_hash(client, make_doc):
    """
    同一篇内容、不同的原始空白 → 判为重复。

    这正是「必须先标准化、后哈希」的证据：不先标准化，``"标题"`` 与 ``" 标题 "``、
    ``"第一步    第二步"`` 与 ``"第一步 第二步"`` 会算出 **两个不同的 hash** ，判重直接失效。
    """
    r1 = await client.post(
        "/api/v1/documents",
        json=make_doc(title="部署手册", content="第一步    第二步"),
    )
    assert r1.status_code == 201

    r2 = await client.post(
        "/api/v1/documents",
        json=make_doc(title=" 部署手册 ", content="第一步 第二步"),
    )
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]


async def test_empty_after_normalize_is_422(client, make_doc):
    """原始内容非空、但标准化后为空 → 拒收（422）。"""
    r = await client.post("/api/v1/documents", json=make_doc(content="   <p></p>  "))
    assert r.status_code == 422


async def test_missing_required_field_is_422(client):
    r = await client.post("/api/v1/documents", json={"source": "feishu", "title": "缺正文"})
    assert r.status_code == 422


async def test_external_id_is_required(client, make_doc):
    """
    阶段 F 起 ``external_id`` **必填** —— 它就是「文档身份」。
    没有它，系统分不清「同一篇改过了」和「这是新的一篇」。
    """
    doc = make_doc()
    doc.pop("external_id")
    r = await client.post("/api/v1/documents", json=doc)
    assert r.status_code == 422


async def test_identity_is_case_sensitive(client, make_doc):
    """
    标识列是 ``utf8mb4_bin`` ⇒ 只差大小写的 ``(source, external_id)`` 是 **两篇** ，不是同一篇。

    这是 §9.4 那条改动的回归测试。继承库默认 ``utf8mb4_unicode_ci`` 时，``ABC-1`` 与 ``abc-1``
    会被唯一索引判成同一个 ID，第二个请求撞成一"更新"（甚至 409），于是这里只会剩 2 篇。

    顺带证明 **列级** collation 真的生效：测试库是按库默认 ``utf8mb4_unicode_ci`` 建的
    （见 ``conftest.prepare_database``），所以只有写在列上的 collation 才能救它。
    """
    for doc in (
        make_doc(external_id="ABC-1"),
        make_doc(external_id="abc-1"),
        make_doc(source="Feishu", external_id="Mixed-1"),
        make_doc(source="feishu", external_id="Mixed-1"),
    ):
        r = await client.post("/api/v1/documents", json=doc)
        assert r.status_code == 201, r.text

    page = await client.get("/api/v1/documents", params={"limit": 100})
    assert page.status_code == 200
    assert page.json()["total"] == 4
