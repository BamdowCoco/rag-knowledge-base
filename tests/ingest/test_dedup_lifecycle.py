"""
内容索引的生命周期（数据库大改 ①）：它现在是 **「active 内容索引」** ，旧内容会被 **释放** 。

它守的不变式是： **任何时刻 active 内容两两不同** —— 由 ``document_dedup.content_hash`` 唯一索引保证。
所以旧版本被更新 / 删除时，必须把它的内容占位 **释放掉** ，否则一份「已经不在语料里」的内容
会被永远判成重复，而它指向的那个版本早就作废了。

> **注意：内容指纹是 ``sha256(标题 + "\\n" + 正文)`` —— 含标题。**
> 所以下面的"撞车"用例都必须用 **同一个标题** ，否则根本不撞。
"""
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.document_dedup import DocumentDedup

# 撞车用例统一用它：标题一样、内容一样 → 指纹才一样
SAME_TITLE = "同名标题"


async def _indexed_hashes() -> set[str]:
    """当前内容索引里还占着位的内容（= 库里仍 active 的内容）。"""
    async with AsyncSessionLocal() as db:
        rows = await db.execute(select(DocumentDedup.content_hash))
        return set(rows.scalars().all())


async def test_update_releases_the_old_content(client, make_doc):
    """A 的内容 X → A 更新成 Y：X 被释放，别的源再推 X 应该【被准入】而不是判重复。"""
    a = make_doc(source="feishu", external_id="a-1", title=SAME_TITLE, content="这是内容X，够长了吧")
    assert (await client.post("/api/v1/documents", json=a)).status_code == 201
    assert (
        await client.post("/api/v1/documents", json={**a, "content": "这是内容Y，够长了吧"})
    ).status_code == 200

    b = make_doc(
        source="confluence", external_id="b-1", title=SAME_TITLE, content="这是内容X，够长了吧"
    )
    r = await client.post("/api/v1/documents", json=b)
    assert r.status_code == 201                       # X 已经不在语料里了 → 这是新增
    assert (await client.get("/api/v1/documents")).json()["total"] == 2


async def test_delete_releases_the_content(client, make_doc):
    """文档删掉后，它的内容也该放回「可准入」。"""
    a = make_doc(source="feishu", external_id="a-2", title=SAME_TITLE, content="这是内容Z，够长了吧")
    await client.post("/api/v1/documents", json=a)
    await client.post("/api/v1/documents/delete", json={"source": "feishu", "external_id": "a-2"})

    b = make_doc(
        source="confluence", external_id="b-2", title=SAME_TITLE, content="这是内容Z，够长了吧"
    )
    assert (await client.post("/api/v1/documents", json=b)).status_code == 201


async def test_update_into_another_docs_content_is_409(client, make_doc):
    """
    把 A 改成与 B **相同** 的内容 → **409** 。

    这是本次补上的那个漏洞：以前 update 不写内容索引，于是库里能 **静默** 出现两份 active 同内容，
    唯一索引一点没拦住 —— 那样「唯一索引是权威」这句话只在新建时成立，是假话。
    """
    a = make_doc(source="feishu", external_id="a-3", title=SAME_TITLE, content="甲的独有内容，够长")
    b = make_doc(source="yuque", external_id="b-3", title=SAME_TITLE, content="乙的独有内容，够长")
    await client.post("/api/v1/documents", json=a)
    await client.post("/api/v1/documents", json=b)

    r = await client.post("/api/v1/documents", json={**a, "content": "乙的独有内容，够长"})
    assert r.status_code == 409

    # A 原封不动（整笔回滚了）
    assert (await client.get("/api/v1/documents")).json()["total"] == 2


async def test_conflict_also_rolls_back_the_release(client, make_doc):
    """
    撞车时 **旧内容的释放也要一并回滚** —— 否则 A 会既没更新成功、原内容占位还丢了。
    （释放与认领放在 **同一个事务** 里，意义就在这里。）
    """
    a = make_doc(source="feishu", external_id="a-4", title=SAME_TITLE, content="甲的独有内容，够长")
    b = make_doc(source="yuque", external_id="b-4", title=SAME_TITLE, content="乙的独有内容，够长")
    await client.post("/api/v1/documents", json=a)
    await client.post("/api/v1/documents", json=b)

    assert (
        await client.post("/api/v1/documents", json={**a, "content": "乙的独有内容，够长"})
    ).status_code == 409

    # A 的原内容必须还在语料里 → 别的文档推它仍然回「已存在」
    c = make_doc(
        source="confluence", external_id="c-4", title=SAME_TITLE, content="甲的独有内容，够长"
    )
    assert (await client.post("/api/v1/documents", json=c)).status_code == 200


async def test_new_document_hitting_existing_content_is_still_200(client, make_doc):
    """新建撞车的语义 **没变** ：回 200 并指向已有那篇（那不是冲突，是"这篇已经在库里了"）。"""
    a = make_doc(source="feishu", external_id="a-5", title=SAME_TITLE, content="共用的内容，够长")
    first = await client.post("/api/v1/documents", json=a)
    assert first.status_code == 201

    b = make_doc(source="confluence", external_id="b-5", title=SAME_TITLE, content="共用的内容，够长")
    r = await client.post("/api/v1/documents", json=b)
    assert r.status_code == 200
    assert r.json()["id"] == first.json()["id"]
    assert (await client.get("/api/v1/documents")).json()["total"] == 1


async def test_index_only_holds_active_content(client, make_doc):
    """索引里只该有「仍 active 的内容」—— 更新后旧 hash 必须从里面消失。"""
    a = make_doc(source="feishu", external_id="a-6", title=SAME_TITLE, content="旧内容，够长了吧")
    await client.post("/api/v1/documents", json=a)
    before = await _indexed_hashes()
    assert len(before) == 1

    await client.post("/api/v1/documents", json={**a, "content": "新内容，够长了吧"})

    after = await _indexed_hashes()
    assert before.isdisjoint(after)      # 旧的被释放了
    assert len(after) == 1               # 新的占上了
