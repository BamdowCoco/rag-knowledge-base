"""
``SKIP LOCKED``：并发认领拿到的批次 **互不相交** 。

这是本仓唯一一条"跟时序有关"的测试，所以只断言 **不相交** 这个不变式，
不去断言具体的切分结果（那几个 worker 谁拿几块，是不确定的）。

注意它 **天然可能 flaky 成"通过"** ：只有当两个 ``SELECT ... FOR UPDATE`` 真的在时间上重叠，
``SKIP LOCKED`` 才起作用；不重叠也能通过。所以它更像"抓回归"（有人把 skip_locked 去掉时
会开始出现交集），而不是"证明并发正确"。要稳定复现，得在两次查询之间插人工延迟。
"""
import asyncio

from app.db.session import AsyncSessionLocal
from app.repositories.chunk_repo import ChunkRepo


async def test_concurrent_claims_are_disjoint(client, make_doc):
    for _ in range(5):                       # 5 篇 → 5 个 chunk（每篇 1 块）
        await client.post("/api/v1/documents", json=make_doc())

    async def claim(limit: int) -> set[int]:
        async with AsyncSessionLocal() as db:      # 每个 worker 一个独立 session/连接
            rows = await ChunkRepo(db).claim_pending(limit)
            return {r.id for r in rows}

    results = await asyncio.gather(*(claim(3) for _ in range(4)))

    for i in range(len(results)):
        for j in range(i + 1, len(results)):
            overlap = results[i] & results[j]
            assert not overlap, f"两个 worker 拿到了同一批：{overlap}"

    # 全都认领出去后，再认领应该拿不到
    assert await claim(10) == set()
