"""
分块仓储：认领 / 完成 / 失败 / 回收 —— 阶段 C 的任务分发。

这里用 MySQL 的行锁做「极简任务队列」：``SELECT ... FOR UPDATE SKIP LOCKED``。
它和分布式锁的分工正好形成对照：

    锁（Redis）          = 这件事只能一个人做（保护临界区）
    SKIP LOCKED（MySQL） = 这堆活大家分着做（分发任务）

``SKIP LOCKED`` 让每个 worker 拿到 **不同的那一批** ，天然不重复。
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document_chunk import DocumentChunk, EmbeddingStatus
from app.models.document_clean import DocumentClean
from app.utils.time import utcnow

logger = logging.getLogger(__name__)


@dataclass
class ChunkView:
    """
    chunk 的读模型：chunk 本体 + 所属 clean 版本的元数据。

    为什么不用 ORM 行：认领一提交，行锁就释放了，下游要的是一份
    「不必再碰会话」的快照；检索回表同理 —— 正文来自 chunk，
    标题 / 来源 / URL / 版本来自 clean。
    """

    id: int
    document_id: int
    chunk_index: int
    content: str
    token_count: int
    claim_token: str | None
    source: str
    external_id: str | None
    title: str
    url: str | None
    language: str | None
    tags: list[str] | None
    content_hash: str
    clean_version: int


class ChunkRepo:
    def __init__(self, db: AsyncSession) -> None:
        """
        绑定请求级会话；认领/回收等方法会自行 commit（任务队列需要立即释放行锁）。

        :param db: 请求级异步会话。
        """
        self.db = db

    async def claim_pending(
        self, limit: int, max_attempts: int = 3
    ) -> list[ChunkView]:
        """
        认领一批待向量化的 chunk，并置为 processing。

        SKIP LOCKED 是关键：已被别人锁住的行 **直接跳过** ，所以并发调用拿到的是
        互不相交的两批。朴素写法（先 SELECT 再 UPDATE）会让两个 worker 拿到同一批。

        认领时写入 ``claim_token`` —— 之后只有拿着这个 token 的 worker 才能完成 / 失败，
        防止被超时回收、任务被别人重领之后，慢 worker 再用旧结果覆盖新结果。

        :param limit: 本轮最多认领多少块。
        :param max_attempts: 失败次数达到它的块不再认领（毒丸）。
        :return: 被认领的块及所属版本元数据（本方法内部 commit 释放行锁）。
        """
        now = utcnow()
        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.embedding_status == EmbeddingStatus.PENDING,
                # 排除已作废的块 —— 所属版本已经不是当前版本了，再向量化就是白花钱
                DocumentChunk.superseded_at.is_(None),
                # 失败到上限的永远不认；正常块 attempts 恒为 0 < max_attempts
                DocumentChunk.embedding_attempts < max_attempts,
                # 退避期没到的不认；NULL 表示没失败过，立即可认领
                or_(
                    DocumentChunk.next_attempt_at.is_(None),
                    DocumentChunk.next_attempt_at <= now,
                ),
            )
            .order_by(DocumentChunk.id)          # 稳定的顺序，避免长期饿死某些行
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        for row in rows:
            row.embedding_status = EmbeddingStatus.PROCESSING
            row.claim_token = str(uuid.uuid4())
            row.updated_at = now
        await self.db.commit()                  # 提交才释放行锁

        if not rows:
            return []

        # 元数据一次批量取回来，别在循环里逐块查
        document_ids = list(dict.fromkeys(row.document_id for row in rows))
        cleans = {
            clean.id: clean
            for clean in (
                await self.db.execute(
                    select(DocumentClean).where(DocumentClean.id.in_(document_ids))
                )
            ).scalars().all()
        }
        views: list[ChunkView] = []
        for row in rows:
            clean = cleans.get(row.document_id)
            if clean is None:
                # FK 保证理论上不会发生；真发生就跳过这一块（它会被超时回收再试），
                # 不让整批任务跟着失败
                logger.warning(
                    "认领到的 chunk %s 找不到所属 clean 版本 id=%s，跳过",
                    row.id,
                    row.document_id,
                )
                continue
            views.append(
                ChunkView(
                    id=row.id,
                    document_id=row.document_id,
                    chunk_index=row.chunk_index,
                    content=row.content,
                    token_count=row.token_count,
                    claim_token=row.claim_token,
                    source=clean.source,
                    external_id=clean.external_id,
                    title=clean.title,
                    url=clean.url,
                    language=clean.language,
                    tags=clean.tags,
                    content_hash=clean.content_hash,
                    clean_version=clean.clean_version,
                )
            )
        return views

    async def get_active_views(self, chunk_ids: list[int]) -> list[ChunkView]:
        """
        按 id 批量回表，只返回 **chunk 与所属版本都未作废** 的行。

        检索侧用它做最终校验：Qdrant 的删除是异步的，可能仍召回「已作废但向量还没删」
        的点；正文只在 MySQL 里，所以必须回表 + 过滤。

        :param chunk_ids: 候选 chunk id（通常来自向量检索结果）。
        :return: 仍处于 active 状态的块；顺序按传入 ``chunk_ids``，缺失的 id 直接跳过。
        """
        if not chunk_ids:
            return []
        stmt = (
            select(DocumentChunk, DocumentClean)
            .join(DocumentClean, DocumentChunk.document_id == DocumentClean.id)
            .where(
                DocumentChunk.id.in_(chunk_ids),
                DocumentChunk.superseded_at.is_(None),
                DocumentClean.superseded_at.is_(None),
            )
        )
        by_id = {
            chunk.id: ChunkView(
                id=chunk.id,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                token_count=chunk.token_count,
                claim_token=chunk.claim_token,
                source=clean.source,
                external_id=clean.external_id,
                title=clean.title,
                url=clean.url,
                language=clean.language,
                tags=clean.tags,
                content_hash=clean.content_hash,
                clean_version=clean.clean_version,
            )
            for chunk, clean in (await self.db.execute(stmt)).all()
        }
        # dict.fromkeys 去重且保序，保证返回顺序与向量召回顺序一致
        return [by_id[cid] for cid in dict.fromkeys(chunk_ids) if cid in by_id]

    async def reclaim_stale(self, older_than: datetime) -> int:
        """
        把卡在 processing 太久的 chunk 放回 pending，返回回收条数。

        **这一环最容易漏** ：worker 领了任务然后崩了，那条就永远卡在 processing ——
        没人会再碰它。没有回收，任务队列会慢慢"漏"掉一部分活。

        回收同时清掉 ``claim_token`` 与 ``next_attempt_at``：前者让那个还没死透的旧 worker
        再回报时校验失败（fencing），后者保证它立刻可以被重新认领。

        :param older_than: ``updated_at`` 早于它的 processing 块会被放回 pending。
        :return: 本次回收的条数。
        """
        stmt = (
            update(DocumentChunk)
            .where(
                DocumentChunk.embedding_status == EmbeddingStatus.PROCESSING,
                DocumentChunk.updated_at < older_than,
                # 已作废的不必再放回 pending —— 反正 claim_pending 也不认领它
                DocumentChunk.superseded_at.is_(None),
            )
            .values(
                embedding_status=EmbeddingStatus.PENDING,
                claim_token=None,
                next_attempt_at=None,
                updated_at=utcnow(),
            )
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return result.rowcount or 0

    async def mark_done(
        self,
        chunk_id: int,
        *,
        claim_token: str | None = None,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
    ) -> tuple[str, DocumentChunk | None]:
        """
        把某个 chunk 标成 done，并记下模型 / 维度 / 完成时刻。

        返回 ``(reason, row)``，由 service 翻译成业务异常 —— 仓储层不认识业务异常：

        - ``not_found``     ：没有这个 id。
        - ``token_mismatch``：认领令牌对不上（已被回收后重领），拒绝覆盖。
        - ``done``          ：已经是 done 且 token 空 / 匹配，幂等成功。
        - ``ok``            ：本次真的更新了。

        **完成时故意保留 ``claim_token``** ：worker 收到响应前崩溃、重试 complete 时，
        上面的 ``done`` 分支会返回幂等成功；下一次 ``claim`` 写入新 token 才会让旧 worker 失效。

        不传 ``claim_token`` 时保持旧行为（任何状态都可标 done），兼容旧调用方。

        :param chunk_id: ``document_chunk.id``。
        :param claim_token: 认领时下发的令牌；``None`` 表示不校验。
        :param embedding_model: 产生该向量的模型（有值才写）。
        :param embedding_dim: 向量维度（有值才写）。
        :return: ``(reason, 行)``；``not_found`` 时行为 ``None``。
        """
        row = (
            await self.db.execute(
                select(DocumentChunk)
                .where(DocumentChunk.id == chunk_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return "not_found", None
        if claim_token is not None and row.claim_token != claim_token:
            # 行锁要主动释放：这里不改数据，commit 即可
            await self.db.commit()
            return "token_mismatch", row
        if row.embedding_status == EmbeddingStatus.DONE:
            await self.db.commit()
            return "done", row

        now = utcnow()
        row.embedding_status = EmbeddingStatus.DONE
        row.embedded_at = now
        row.updated_at = now
        if embedding_model is not None:
            row.embedding_model = embedding_model
        if embedding_dim is not None:
            row.embedding_dim = embedding_dim
        await self.db.commit()
        return "ok", row

    async def mark_failed(
        self,
        chunk_id: int,
        *,
        claim_token: str,
        error: str | None,
        max_attempts: int,
        backoff_seconds: int,
    ) -> tuple[str, DocumentChunk | None, int]:
        """
        记录一次失败：``embedding_attempts += 1``，未达上限退回 pending 并退避，
        达上限转 ``failed`` 终态（毒丸）。

        返回 ``(reason, row, attempts)``：

        - ``not_found``     ：没有这个 id。
        - ``token_mismatch``：认领令牌对不上，拒绝写入。
        - ``noop``          ：已经是 done / failed，重复回报按幂等处理。
        - ``ok``            ：本次真的更新了。

        :param chunk_id: ``document_chunk.id``。
        :param claim_token: 认领时下发的令牌（必填）。
        :param error: 失败原因；入库前截断到 2000 字符。
        :param max_attempts: 失败次数上限（由 service 从配置传入，仓储不依赖配置）。
        :param backoff_seconds: 指数退避基数。
        :return: ``(reason, 行, 当前失败次数)``。
        """
        row = (
            await self.db.execute(
                select(DocumentChunk)
                .where(DocumentChunk.id == chunk_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return "not_found", None, 0
        if row.claim_token != claim_token:
            await self.db.commit()
            return "token_mismatch", row, row.embedding_attempts
        if row.embedding_status in (EmbeddingStatus.DONE, EmbeddingStatus.FAILED):
            await self.db.commit()
            return "noop", row, row.embedding_attempts

        attempts = row.embedding_attempts + 1
        row.embedding_attempts = attempts
        now = utcnow()
        if attempts >= max_attempts:
            row.embedding_status = EmbeddingStatus.FAILED
            row.next_attempt_at = None
        else:
            row.embedding_status = EmbeddingStatus.PENDING
            # 5s / 10s / 20s … —— 退避让瞬时故障自愈，又不至于把队列拖死
            row.next_attempt_at = now + timedelta(
                seconds=backoff_seconds * (2 ** (attempts - 1))
            )
        row.claim_token = None                    # 任务回到队列，旧 worker 就此失去写权限
        row.embedding_error = error[:2000] if error else None
        row.updated_at = now
        await self.db.commit()
        return "ok", row, attempts

    # ---------- 更新 / 删除语义（阶段 F）----------

    async def supersede_by_document(self, document_id: int) -> int:
        """
        作废某个版本的全部块，返回条数。

        故意 **不动 ``retracted_at``** —— 哪些块需要下游撤回，交给 ``claim_retractions``
        按「已 done 且未 retracted」自己筛。于是「作废时还停在 pending 的块不用撤回」
        （下游从没见过它）这件事 **自动成立** ，不用在这里特判。

        :param document_id: 文档版本 id（``document_chunk.document_id``）。
        :return: 实际作废的块数。
        """
        stmt = (
            update(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.superseded_at.is_(None),
            )
            .values(superseded_at=utcnow())
        )
        result = await self.db.execute(stmt)
        return result.rowcount or 0

    async def claim_retractions(self, limit: int) -> list[DocumentChunk]:
        """
        认领一批「要下游把向量删掉」的块。

        **它和补齐流不一样：这里没有 in-flight 状态。** 因为删向量是 **幂等** 的
        （同一个向量删两次没差别），所以允许多个 worker 拿到同一批、下游重复删 ——
        换来的是「worker 崩了任务也不会静默丢失，下一轮自然重试」。
        也就是说它 **不共享** ``SKIP LOCKED`` 的「各拿一批」语义，这是刻意的取舍。

        只在 **已 done** 的块上产生任务：没被认领过的 pending 块压根没向量可删。

        :param limit: 本轮最多返回多少块。
        :return: 已作废、已 done、尚未确认删除的块；可能被多个 worker 重复拿到。
        """
        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.superseded_at.is_not(None),
                DocumentChunk.retracted_at.is_(None),
                DocumentChunk.embedding_status == EmbeddingStatus.DONE,
            )
            .order_by(DocumentChunk.id)
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def mark_retracted(self, chunk_id: int) -> bool:
        """
        下游确认「该向量已删」（幂等：重复确认不会重复更新）。

        :param chunk_id: ``document_chunk.id``。
        :return: 是否本次真的更新了；``False`` 表示没这个 id 或已经确认过。
        """
        stmt = (
            update(DocumentChunk)
            .where(DocumentChunk.id == chunk_id, DocumentChunk.retracted_at.is_(None))
            .values(retracted_at=utcnow())
        )
        result = await self.db.execute(stmt)
        await self.db.commit()
        return (result.rowcount or 0) > 0
