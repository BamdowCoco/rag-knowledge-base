"""
管道积压指标刷新：把「待向量化的 active chunk 数」定时写进 Prometheus Gauge。

**为什么不让请求路径顺手算**：``count(*)`` 要扫 ``document_chunk`` 的索引，放在请求里
会让检索 / 问答平白多一次数据库往返；而积压量本来就是运维看的低频数字，定期刷就够。

**为什么 rag 角色也要起**：这个循环只读 MySQL，不碰 Redis / Qdrant，rag 进程本来就连着
同一个库；让两个角色都暴露一致的 ``rag_pipeline_pending_chunks``，看板才不会在
切换实例时忽高忽低。

**失败只告警不退出**：数据库抖动、迁移未完成都会让某一次查询失败，但下一次可能就好了；
让循环因为一次异常彻底死掉，指标会静默停更 —— 那比暂时缺一个点更难发现。
"""
import asyncio
import logging

from sqlalchemy import text

from app.core.config import settings
from app.core.metrics import set_pipeline_pending_chunks
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# 只数「当前有效、且还没向量化」的 chunk。与 ingest 侧的 active chunk 口径一致
# （``superseded_at IS NULL``），否则撤回中的旧版本会把积压量算大。
_PENDING_SQL = text(
    "SELECT COUNT(*) FROM document_chunk "
    "WHERE superseded_at IS NULL AND embedding_status = 'pending'"
)


async def refresh_pending_chunks() -> int:
    """
    查一次待向量化 chunk 数并写入 Gauge。

    **自己开 session** —— 后台循环没有请求级依赖可借。

    :return: 本次查到的待处理条数。
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(_PENDING_SQL)
        count = int(result.scalar_one())
    set_pipeline_pending_chunks(count)
    return count


async def metrics_loop() -> None:
    """
    后台循环：先刷一次，再每 ``metrics_refresh_seconds`` 秒刷一次，直到任务被取消。

    首次启动立即刷，保证 ``/metrics`` 在第一个周期内就有真实值，而不是 0 挂一分钟。
    无限循环，正常路径不返回；``CancelledError`` 原样抛出以完成优雅关停。
    """
    logger.info("指标刷新循环已启动，每 %s 秒刷新一次", settings.metrics_refresh_seconds)
    while True:
        try:
            count = await refresh_pending_chunks()
            logger.debug("待向量化 chunk 数：%d", count)
        except asyncio.CancelledError:
            raise       # 取消要原样抛出（CancelledError 是 BaseException，不会被下面吞掉）
        except Exception:
            logger.warning("刷新待向量化 chunk 指标失败（不影响主流程，下一轮再试）", exc_info=True)
        await asyncio.sleep(settings.metrics_refresh_seconds)
