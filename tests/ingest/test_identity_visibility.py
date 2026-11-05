"""
身份路径的可见性：``get_source(..., FOR UPDATE)`` 是当前读，
``get_active_version`` 也必须当前读 —— 否则会误判成「身份已存在 + 新内容」（假 409）。

这条测试用两个独立 session 确定性地复现，不依赖并发压测碰运气。
"""
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.document_source import DocumentSource
from app.repositories.document_repo import DocumentRepo


async def test_for_update_sees_version_committed_after_snapshot(client, make_doc):
    doc = make_doc(source="feishu", external_id="visibility-race")

    async with AsyncSessionLocal() as observer:
        # 1. 先做一次普通读 —— REPEATABLE READ 的一致性快照在这一刻固定下来
        await observer.execute(select(DocumentSource).where(DocumentSource.id == -1))

        # 2. 另一个连接真正建文档并提交（发生在 observer 的快照之后）
        r = await client.post("/api/v1/documents", json=doc)
        assert r.status_code == 201

        # 3. 用独立 session 取出身份 id（observer 自己的快照看不到它）
        async with AsyncSessionLocal() as reader:
            src = (
                await reader.execute(
                    select(DocumentSource).where(
                        DocumentSource.source == doc["source"],
                        DocumentSource.external_id == doc["external_id"],
                    )
                )
            ).scalar_one()
            source_id = src.id

        repo = DocumentRepo(observer)

        # 4. 快照读：看不到快照之后提交的行 —— 这正是假 409 的来源
        assert await repo.get_active_version(source_id, for_update=False) is None

        # 5. 当前读：看得到
        current = await repo.get_active_version(source_id, for_update=True)
        assert current is not None
        assert current.source_id == source_id

        await observer.rollback()
