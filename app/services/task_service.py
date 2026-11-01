"""
向量化任务的认领与回报（阶段 C；P0 起带 claim token / 失败重试）。

下游 worker 的用法：

    POST /api/v1/chunks/claim          → 拿一批待处理的 chunk（带 claim_token 与元数据）
    （去做 embedding —— 这里是最慢的一步，也是要花钱的一步）
    POST /api/v1/chunks/{id}/complete  → 回报完成（带 token / 模型 / 维度）
    POST /api/v1/chunks/{id}/fail      → 回报失败（带 token 与原因）

为什么用 DB 当队列、而不引 Redis/消息队列：本项目的数据量级不需要；
而 ``SKIP LOCKED`` 恰好提供了「多个 worker 各拿一批、互不重复」的语义。
真上规模时再换 MQ/RabbitMQ，接口形状可以不变。

注意： **超时回收不在这里** ，它是个独立的后台循环（``reclaimer.py``）—— 为什么拆出去，
见那个文件的说明。
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ChunkClaimMismatchError, ChunkNotFoundError
from app.models.document_chunk import DocumentChunk
from app.repositories.chunk_repo import ChunkRepo, ChunkView

logger = logging.getLogger(__name__)


class TaskService:
    def __init__(self, db: AsyncSession) -> None:
        """
        绑定请求级会话；认领/完成/失败/撤回都直接落到 ``ChunkRepo``。

        :param db: 请求级异步会话。
        """
        self.db = db
        self.repo = ChunkRepo(db)

    async def claim(self, limit: int) -> list[ChunkView]:
        """
        认领一批任务。超时回收由独立的 reclaim_loop 负责（见 reclaimer.py）。

        :param limit: 本轮最多认领多少块。
        :return: 已置为 ``processing`` 的块及所属版本元数据；没有待办时返回空列表。
        """
        rows = await self.repo.claim_pending(limit, max_attempts=settings.max_embed_attempts)
        logger.info("认领 %d 个待向量化 chunk", len(rows))
        return rows

    async def claim_retractions(self, limit: int) -> list[DocumentChunk]:
        """
        认领一批「要下游把向量删掉」的块。

        注意它和 ``claim`` **不是一回事** ：撤回没有 in-flight 状态，多个 worker 可能拿到同一批
        （删向量是幂等的）—— 详细取舍见 ``chunk_repo.claim_retractions``。

        :param limit: 本轮最多返回多少块。
        :return: 待撤回的块（所属版本已作废、且下游尚未确认删除）。
        """
        rows = await self.repo.claim_retractions(limit)
        if rows:
            logger.info("认领 %d 个待撤回 chunk（所属版本已作废）", len(rows))
        return rows

    async def complete(
        self,
        chunk_id: int,
        claim_token: str | None = None,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
    ) -> DocumentChunk:
        """
        回报某个 chunk 已完成。

        带 ``claim_token`` 时做 fencing 校验；已 done 且 token 匹配视为幂等成功。
        不带 token 时保持旧行为（任何状态都可标 done）。

        :param chunk_id: ``document_chunk.id``。
        :param claim_token: 认领时下发的令牌；``None`` 表示不校验（兼容旧调用方）。
        :param embedding_model: 产生该向量的模型。
        :param embedding_dim: 向量维度。
        :return: 更新后的 chunk 行（已 ``expire_all`` 重新读取）。
        :raises ChunkNotFoundError: id 不存在。
        :raises ChunkClaimMismatchError: 认领令牌已失效（被回收后重新认领）。
        """
        reason, _ = await self.repo.mark_done(
            chunk_id,
            claim_token=claim_token,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
        )
        if reason == "not_found":
            logger.info("回报完成失败：chunk 不存在 id=%s", chunk_id)
            raise ChunkNotFoundError()
        if reason == "token_mismatch":
            logger.warning("回报完成被拒：claim_token 已失效 id=%s", chunk_id)
            raise ChunkClaimMismatchError()
        logger.info("chunk %s 已完成（%s）", chunk_id, reason)

        # 行对象可能已在会话里；先失效再取，拿到的才是库里的最新状态
        self.db.expire_all()
        obj = await self.db.get(DocumentChunk, chunk_id)
        assert obj is not None                      # 刚刚更新成功过，必然存在
        return obj

    async def fail(
        self, chunk_id: int, claim_token: str, error: str | None = None
    ) -> DocumentChunk:
        """
        回报某个 chunk 向量化失败：次数 +1，未达上限退回 pending（带退避），
        达上限转 ``failed`` 终态。

        :param chunk_id: ``document_chunk.id``。
        :param claim_token: 认领时下发的令牌（必填）。
        :param error: 失败原因（入库前截断）。
        :return: 更新后的 chunk 行。
        :raises ChunkNotFoundError: id 不存在。
        :raises ChunkClaimMismatchError: 认领令牌已失效。
        """
        reason, _, attempts = await self.repo.mark_failed(
            chunk_id,
            claim_token=claim_token,
            error=error,
            max_attempts=settings.max_embed_attempts,
            backoff_seconds=settings.embed_backoff_seconds,
        )
        if reason == "not_found":
            logger.info("回报失败失败：chunk 不存在 id=%s", chunk_id)
            raise ChunkNotFoundError()
        if reason == "token_mismatch":
            logger.warning("回报失败被拒：claim_token 已失效 id=%s", chunk_id)
            raise ChunkClaimMismatchError()
        logger.info("chunk %s 向量化失败（第 %d 次，%s）", chunk_id, attempts, reason)

        self.db.expire_all()
        obj = await self.db.get(DocumentChunk, chunk_id)
        assert obj is not None
        return obj

    async def retract(self, chunk_id: int) -> DocumentChunk:
        """
        回报「该向量已从向量库删掉」。

        ``mark_retracted`` 用的是 Core update()，它绕过 ORM、不会刷新身份映射里的对象；
        所以先 ``expire_all()``，再取才是库里的最新状态。

        :param chunk_id: ``document_chunk.id``。
        :return: 更新后的 chunk 行。
        :raises ChunkNotFoundError: id 不存在，或已经确认过撤回。
        """
        if not await self.repo.mark_retracted(chunk_id):
            logger.info("回报撤回失败：chunk 不存在或已确认过 id=%s", chunk_id)
            raise ChunkNotFoundError()
        logger.info("chunk %s 的向量已撤回", chunk_id)

        self.db.expire_all()
        obj = await self.db.get(DocumentChunk, chunk_id)
        assert obj is not None
        return obj
