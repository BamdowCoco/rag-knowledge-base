"""
文档业务编排。

阶段 3：list_documents（不加缓存）。
阶段 5：ingest() —— 标准化 → 判重(快路径) → 三表落库。
阶段 6：给 ingest 的临界区套上按 hash 粒度的分布式锁。
阶段 7：列表与单篇都走 Cache-Aside；写入成功后 bump 列表版本号。
阶段 9：service 不再抛 HTTPException（改为业务异常，HTTP 映射在 core/exceptions.py）；补处理日志；插入 AI 接缝 enrich（失败不阻断入库）。
阶段 B：落库后接上【切块】；新增分块查询（纯 TTL 缓存）。
阶段 D：重复改为「不是错误」（返回已存在那篇）；新增批量摄入 bulk_ingest（pipeline 判重）。
"""
import json
import logging

from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    ContentConflictError,
    DocumentInProgressError,
    DocumentNotFoundError,
    EmptyContentError,
)
from app.db.retry import is_transient_error, with_transient_retry
from app.models.document_clean import DocumentClean
from app.repositories.chunk_repo import ChunkRepo
from app.repositories.document_repo import DocumentRepo
from app.repositories.dedup_repo import DedupRepo
from app.schemas.chunk import ChunkOut
from app.schemas.document import (
    BulkItemResult,
    BulkResult,
    DocumentCreate,
    DocumentDelete,
    DocumentOut,
)
from app.services.bulk_planner import BulkPlanner, PostCommit
from app.services.cache_service import (
    cache_delete,
    cache_get,
    cache_set,
    chunk_list_cache_key,
    document_cache_key,
    document_version_key,
    bump_list_version,
    get_list_version,
    list_cache_key,
    stats_cache_key,
)
from app.services.chunk_service import make_chunks
from app.services.normalize_service import normalize_document
from app.services.dedup_service import (
    DEDUP_KEY,
    check_and_mark,
    get_rejected_count,
    incr_rejected,
    incr_rejected_by,
    release_mark,
)
from app.services.lock_service import LockNotAcquired, document_lock
from app.utils.time import beijing_today_utc_range, utcnow

logger = logging.getLogger(__name__)


class _SliceAbort(Exception):
    """片内发现「必须交给逐篇路径重新分类」的情况（例如更新撞上同片已认领的内容）。"""


class DocumentService:
    def __init__(self, db: AsyncSession, redis: Redis) -> None:
        """
        绑定请求级会话与共享 Redis 客户端，并初始化各仓储与多行 planner。

        :param db: 请求级异步会话；事务边界由本服务的各入口控制。
        :param redis: 共享 Redis 客户端（快路径判重、分布式锁、缓存/版本号）。
        """
        self.db = db
        self.redis = redis          # 快路径判重、分布式锁、缓存/版本号
        self.repo = DocumentRepo(db)
        self.dedup_repo = DedupRepo(db)
        self.chunk_repo = ChunkRepo(db)
        self.planner = BulkPlanner(db)

    # ---------- 读 ----------

    async def list_documents(
        self, *, page: int, limit: int, source: str | None = None, q: str | None = None
    ) -> tuple[list[dict], int]:
        """
        列表查询（Cache-Aside）。返回 (本页数据 dict 列表, 满足条件的总数)。

        带 ``q`` 的请求【不进缓存】：q 是自由文本、键空间无上限（能被刷爆），
        且命中率极低 —— 直接查库反而更划算。

        :param page: 页码，从 1 开始。
        :param limit: 每页条数。
        :param source: 按来源过滤；``None`` 表示不过滤。
        :param q: 标题/正文关键词；一旦传入就绕过缓存（原因见上）。
        :return: ``(本页文档 dict 列表, 满足条件的总数)``，dict 已转成 JSON 友好格式。
        """
        cache_key: str | None = None
        if q is None:
            version = await get_list_version(self.redis)
            cache_key = list_cache_key(version, page=page, limit=limit, source=source)
            cached = await cache_get(self.redis, cache_key)
            if cached is not None:
                return cached["items"], cached["total"]

        items = await self.repo.list_documents(page=page, limit=limit, source=source, q=q)
        total = await self.repo.count_documents(source=source, q=q)
        # 转成 JSON 友好的 dict（mode="json" 让 datetime 变字符串），缓存和返回都用它
        payload = {
            "items": [DocumentOut.model_validate(o).model_dump(mode="json") for o in items],
            "total": total,
        }
        if cache_key is not None:
            await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload["items"], payload["total"]

    async def get_document(self, document_id: int) -> DocumentOut:
        """
        单篇查询（Cache-Aside）。

        注意：本项目文档写入后永不修改，所以单条缓存用纯 TTL 就够，
        不需要「写时删」—— 失效策略取决于数据可变性，可变数据才需要写时失效。

        :param document_id: 文档当前版本的主键 ``document_clean.id``。
        :return: 文档出参；命中缓存时直接反序列化返回。
        :raises DocumentNotFoundError: 该 id 不存在。
        """
        cache_key = document_cache_key(document_id)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return DocumentOut.model_validate(cached)

        obj = await self.repo.get_by_id(document_id)
        if obj is None:
            logger.info("文档不存在 id=%s", document_id)
            raise DocumentNotFoundError()

        out = DocumentOut.model_validate(obj)
        await cache_set(self.redis, cache_key, out.model_dump(mode="json"), ttl=settings.cache_ttl)
        return out

    async def get_stats(self) -> dict:
        """
        统计：总数 / 按来源 top10 / 今日新增 / 判重拒收累计 / **chunk 状态分布** 。

        整体缓存，与列表共用同一个版本号 —— 一次 INCR 同时作废两者。
        「今日」按【北京时间】算（库里存的是 UTC，所以这里做一次边界换算）。

        注意 chunk_status 的陈旧度：版本号只在【文档写入】时 +1，而 claim/complete
        不动版本号，所以「向量化积压」最多陈旧一个 cache_ttl（默认 60s）。
        看板指标 60s 粒度可接受，也与 total/today_count 的陈旧度一致 —— 不为它破例。

        :return: 统计字段 dict（total / by_source / today_count / dedup_rejected /
            chunk_status / bench_count）。
        """
        version = await get_list_version(self.redis)
        cache_key = stats_cache_key(version)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            # 兼容上线前的旧缓存：旧 payload 的 by_source 里可能还带着 bench。
            by_source_cached = [
                item
                for item in cached.get("by_source", [])
                if item.get("source") != "bench"
            ]
            bench_cached = next(
                (
                    item.get("count", 0)
                    for item in cached.get("by_source", [])
                    if item.get("source") == "bench"
                ),
                0,
            )
            cached["by_source"] = by_source_cached
            cached.setdefault("bench_count", bench_cached)
            return cached

        today_start, today_end = beijing_today_utc_range()
        raw_by_source = await self.repo.count_by_source(limit=10)
        # 压测数据单独计数：18 万条 bench 会把运维页的真实来源条形压平，默认不进 by_source。
        bench_count = next((n for source, n in raw_by_source if source == "bench"), 0)
        by_source = [(source, n) for source, n in raw_by_source if source != "bench"]
        chunk_status = await self.repo.count_chunks_by_status()
        payload = {
            "total": await self.repo.count_documents(),
            "by_source": [{"source": s, "count": n} for s, n in by_source],
            "today_count": await self.repo.count_since(today_start, today_end),
            "dedup_rejected": await get_rejected_count(self.redis),
            "chunk_status": [{"status": s, "count": n} for s, n in chunk_status],
            "bench_count": bench_count,
        }
        await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload

    async def list_chunks(self, document_id: int, *, page: int, limit: int) -> tuple[list[dict], int]:
        """
        分块列表（Cache-Aside，纯 TTL）。

        为什么不像列表/统计那样用版本号：chunk 是 **一次写入、永不修改** 的 ——
        没有「写后失效」这回事，TTL 自然过期就够了。
        这正是「失效策略取决于数据可变性」的正面例子。

        :param document_id: 要查的文档当前版本 id。
        :param page: 页码，从 1 开始。
        :param limit: 每页条数。
        :return: ``(本页分块 dict 列表, 分块总数)``。
        :raises DocumentNotFoundError: 文档不存在（空结果时会再确认一次）。
        """
        cache_key = chunk_list_cache_key(document_id, page=page, limit=limit)
        cached = await cache_get(self.redis, cache_key)
        if cached is not None:
            return cached["items"], cached["total"]

        rows = await self.repo.list_chunks(document_id, page=page, limit=limit)
        total = await self.repo.count_chunks(document_id)
        # 空结果时再确认文档是否存在 —— 免得把「id 打错了」显示成「文档存在但没有块」。
        # 只在空结果时多查一次，常见路径不吃这个开销。
        if total == 0 and await self.repo.get_by_id(document_id) is None:
            raise DocumentNotFoundError()

        payload = {
            "items": [ChunkOut.model_validate(r).model_dump(mode="json") for r in rows],
            "total": total,
        }
        await cache_set(self.redis, cache_key, payload, ttl=settings.cache_ttl)
        return payload["items"], payload["total"]

    # ---------- 写 ----------

    async def _persist(
        self,
        raw: dict,
        cleaned: dict,
        *,
        source_id: int,
    ) -> DocumentClean:
        """
        三表顺序落库：raw → clean → dedup（顺带切块）， **只 flush、不 commit** 。

        单篇与批量都要走这一段，但事务边界各自由调用方决定（批量是每篇一个事务），
        所以提交不放在这里。

        **落库时同时认领这份内容** （写 ``document_dedup``）—— 那是 **准入占位** 。
        新建 / 更新 / 复活 **都要认领** ：只有这样，「任何时刻 active 内容两两不同」
        才是条 **真不变式** ，而不是"只在新建时成立"。
        撞车由 ``content_hash`` 的唯一索引拦下，报什么由调用方决定。

        **enrich 那几列这里一个字都不写** —— 它们由后台循环补（见 ``enricher.py``），
        落库时 ``enrich_status`` 还是默认的 ``pending``。这正是「AI 不拦请求」的落点。

        **raw 不再存整包请求体** （2026-10-17 删了 ``payload`` 那列，见 §9.13-a）：
        留痕靠独立列 —— ``source`` / ``external_id`` / ``raw_title`` / ``raw_content`` / ``received_at``
        已经够自描述，且正文因此从三份降到两份。

        :param raw: 原始入参 dict（标准化之前）。
        :param cleaned: ``normalize_document`` 的产物（标题/正文/哈希等）。
        :param source_id: 该文档身份行的 id（``document_source.id``）。
        :return: 新建的 ``document_clean`` 行（已 flush、未 commit）。
        """
        raw_row = await self.repo.insert_raw(
            source=raw["source"],
            external_id=raw.get("external_id"),
            raw_title=raw["title"],
            raw_content=raw["content"],
        )
        clean_row = await self.repo.insert_clean(
            raw_id=raw_row.id,
            source=raw["source"],
            source_id=source_id,
            external_id=raw.get("external_id"),
            content_hash=cleaned["content_hash"],
            title=cleaned["title"],
            content=cleaned["content"],
            author=raw.get("author"),          # enrich 补的署名由后台写回，不在这里
            url=raw.get("url"),
            doc_updated_at=raw.get("doc_updated_at"),
            char_count=cleaned["char_count"],
            clean_version=cleaned["clean_version"],
        )
        # 切块：文档落库后立刻切。chunk 与文档在【同一个事务】里 ——
        # 要么文档和它的块都在，要么都不在，不留半截数据。
        chunks = make_chunks(cleaned)
        await self.repo.insert_chunks(clean_row.id, chunks)

        # 认领这份内容 —— 唯一索引是权威，撞车就抛 IntegrityError，报什么由调用方决定
        await self.dedup_repo.insert_dedup(cleaned["content_hash"], clean_row.id)
        logger.info("落库完成 doc_id=%s 块数=%d", clean_row.id, len(chunks))
        return clean_row

    async def _find_existing(self, content_hash: str) -> DocumentClean | None:
        """
        按内容指纹回读「已存在」的那篇文档（准入检查命中时要把这篇回给调用方）。

        :param content_hash: 标准化后标题+正文的 sha256。
        :return: 持有该内容的当前版本；索引里没有则为 ``None``（幽灵占位的情况）。
        """
        dedup = await self.dedup_repo.get_by_hash(content_hash)
        if dedup is None:
            return None
        return await self.repo.get_by_id(dedup.clean_id)

    async def ingest(self, payload: DocumentCreate) -> tuple[DocumentClean, bool]:
        """
        整条管道：标准化 → **快路径** → 加锁 → 身份判断 → 三表落库 → 提交 → bump 列表版本号。

        只有一条路：``external_id`` 就是文档身份。

        - **快路径** （不查库、不抢锁）：命中且内容没变 → 直接回当前版本
        - **库路径** ：没有身份行 → 过准入检查后新建；有 → 比 hash 决定跳过还是更新

        返回 (文档, 是否新建)，接口层据此回 201 / 200。

        :param payload: 单篇提交的入参（``external_id`` 必填）。
        :return: ``(当前版本行, 是否新建)``。
        :raises EmptyContentError: 标准化后正文为空。
        :raises DocumentInProgressError: 同身份的并发请求正持有分布式锁。
        :raises ContentConflictError: 更新时新内容已被别的身份持有。
        """
        raw = payload.model_dump()
        cleaned = normalize_document(raw)
        content_hash = cleaned["content_hash"]
        logger.info(
            "ingest 开始 source=%s ext=%s hash=%s",
            raw["source"], raw["external_id"], content_hash[:12],
        )

        # 标准化后正文为空 → 拒收。放锁外做，省一次加锁（反正它不涉及竞态）。
        if not cleaned["content"]:
            logger.info("拒收：标准化后正文为空 hash=%s", content_hash[:12])
            raise EmptyContentError()

        # ---- 快路径：最常见的那路流量（重推一篇没变的文档），不查库、不抢锁 ----
        fast = await self._fast_skip(raw, cleaned)
        if fast is not None:
            return fast, False

        lock_id = self._identity_lock_id(raw["source"], raw["external_id"])
        try:
            async with document_lock(self.redis, lock_id):
                # 死锁（1213）/ 锁等待超时（1205）是瞬时的：回滚后重跑通常就成功。
                # 身份锁在重试期间不释放，同身份的并发仍然被挡住。
                clean_row, created, changed = await with_transient_retry(
                    lambda: self._apply_identified_committed(raw, cleaned),
                    session=self.db,
                )
        except LockNotAcquired:
            logger.info("未拿到锁（同身份正在处理）ext=%s", raw["external_id"])
            raise DocumentInProgressError() from None

        if changed:
            await bump_list_version(self.redis)
        return clean_row, created

    async def _fast_skip(
        self, raw: dict, cleaned: dict, *, count: bool = True
    ) -> DocumentClean | None:
        """
        身份版本快路径：缓存命中且内容没变 → 直接回【当前版本】。

        **它挡的是最常见的流量** ：同步器周期性重推一篇没变的文档。
        没这一步，每次重推都要 ``SELECT ... FOR UPDATE`` 去抢一次行锁 —— 又慢、又互相排队。
        缓存里存 ``{clean_id, content_hash}``：光有 hash 不够，还得有 id 才能取出文档本体回给调用方。

        **失败模式** ：这份缓存错了是 **「漏一次更新」** （把新内容当成没变），
        不是去重 SET 那种「多拒一次」—— 严重性不同，所以它 write-through + TTL 兜底。
        读不到实体行（缓存与库不同步）时 **老实回落** 到库路径，不硬信缓存。

        :param raw: 原始入参 dict。
        :param cleaned: 标准化后的字段 + ``content_hash``。
        :param count: 命中时是否给「判重拒收」计数器 +1。批量入口按最终结果统一计数，
            所以传 ``False``。
        :return: 缓存指向的当前版本；未命中或已作废则 ``None``（调用方回落库路径）。
        """
        cached = await cache_get(
            self.redis, document_version_key(raw["source"], raw["external_id"])
        )
        if not cached or cached.get("content_hash") != cleaned["content_hash"]:
            return None

        # 【必须在 SQL 层过滤 superseded_at】缓存里的 clean_id 可能已被并发更新作废；
        # 只看 ORM 对象属性会漏判（supersede_version 是 Core UPDATE，identity map 是旧值）。
        row = await self.repo.get_active_by_id(cached["clean_id"])
        if row is None:                      # 缓存指向已作废版本 → 回落，别撒谎
            return None

        if count:
            await incr_rejected(self.redis)  # 它确实挡掉了一次重复同步
        logger.info("快路径命中 → 内容未变 ext=%s", raw["external_id"])
        return row

    # ---------- 写：身份路径（阶段 F）----------

    @staticmethod
    def _identity_lock_id(source: str, external_id: str) -> str:
        """
        身份锁的 ID。 **必须按身份锁、不能按内容锁** ——
        同一篇文档"改了内容"前后是两个不同的 hash，按 hash 锁等于各拿各的锁，根本拦不住。

        :param source: 文档来源。
        :param external_id: 源系统里的稳定文档 id。
        :return: Redis 身份锁的 key，形如 ``src:<source>:<external_id>``。
        """
        return f"src:{source}:{external_id}"

    async def _apply_identified_in_tx(
        self,
        raw: dict,
        cleaned: dict,
        *,
        post: PostCommit,
        marks_added: set[str],
        seen_hashes: dict[str, DocumentClean],
        dirty: set[tuple[str, str]],
        count_rejected: bool = True,
        state: dict | None = None,
    ) -> tuple[DocumentClean, bool, bool]:
        """
        身份路径的内核： **只读写 / flush，绝不 commit 或 rollback** 。

        单篇由 ``_apply_identified_committed`` 包一个事务；批量由 ``bulk_ingest``
        把 K 篇放进同一个事务、片末统一提交。

        任何数据库冲突（IntegrityError）都直接抛出 —— 片级负责整片回滚 + 逐篇重放；
        单篇包装器负责 rollback 后的 duplicate / 409 分类。

        :param raw: 原始入参 dict。
        :param cleaned: 标准化后的字段 + ``content_hash``。
        :param post: 收集提交成功后才执行的 Redis 副作用。
        :param marks_added: 本事务里 ``SADD`` 返回 1 的 hash；片回滚时据此补偿。
        :param seen_hashes: 片内 ``content_hash -> clean_row``，用于同片跨身份判重。
        :param dirty: 片内已经写过的身份；命中过的身份不再走快路径。
        :param count_rejected: 重复分支是否立即给计数器 +1；批量入口传 ``False``。
        :param state: 可选出参 dict，回填 ``conflict_is_409`` 供包装器分类。
        :return: ``(当前版本行, 是否新建, active 集合是否有变动)``。
        :raises IntegrityError: 身份或内容唯一约束冲突，由调用方决定回滚与分类。
        """
        source, external_id = raw["source"], raw["external_id"]
        content_hash = cleaned["content_hash"]
        identity = (source, external_id)
        if state is not None:
            state["conflict_is_409"] = False

        src = await self.repo.get_source(source, external_id, for_update=True)
        current = None
        if src is None:
            # 同片内存表：这份内容已经被本事务里的另一篇认领 → 直接算 duplicate，
            # 不等唯一索引在片末 commit 时才炸（那时整片都要回退）。
            known = seen_hashes.get(content_hash)
            if known is not None:
                if count_rejected:
                    await incr_rejected(self.redis)
                return known, False, False

            # 快路径（Redis SADD）只是省一次查库；真正的权威是落库时的唯一索引。
            added = await check_and_mark(self.redis, content_hash)
            if added:
                # 返回 1 的下一行就记录：万一后面抛异常，片回滚时才知道要补偿
                marks_added.add(content_hash)
            else:
                existing = await self._find_existing(content_hash)
                if existing is not None:
                    if count_rejected:
                        await incr_rejected(self.redis)
                    logger.info("准入检查 → 内容已属于别人 hash=%s", content_hash[:12])
                    return existing, False, False
                # Redis 说"见过"、索引里却没有 → 旧占位的幽灵（罕见、自愈），继续按新建走
            src = await self.repo.insert_source(source, external_id)
            created, changed = True, True
        else:
            # 【必须当前读】get_source(... FOR UPDATE) 已经能看到并发刚提交的身份行；
            # 这里若用普通 SELECT（快照读），可能看不到同一时刻写入的版本行 ——
            # 于是误判成「身份已存在 + 新内容」，被 content_hash 唯一索引拦下 → 假 409。
            current = await self.repo.get_active_version(src.id, for_update=True)
            if current is not None and current.content_hash == content_hash:
                if count_rejected:
                    await incr_rejected(self.redis)      # 它确实挡掉了一次重复同步
                logger.info("身份判重 → 内容未变 ext=%s", external_id)
                return current, False, False

            # 更新 / 复活：新内容若已被同片另一篇认领，交给逐篇路径判 409
            if content_hash in seen_hashes:
                raise _SliceAbort(
                    f"同片内容冲突 ext={external_id} hash={content_hash[:12]}"
                )
            created, changed = False, True
            if state is not None:
                state["conflict_is_409"] = True   # 已存在身份 → 内容撞车是冲突
            if src.deleted_at is not None:
                src.deleted_at = None
                logger.info("身份复活 ext=%s", external_id)

        clean_row = await self._persist(raw, cleaned, source_id=src.id)

        if current is not None:
            # 作废旧版本：它的块一并作废；它的内容也要释放 ——
            # 旧内容不再 active，占着位会把"其实已经不在语料里"的内容一直判成重复。
            await self.repo.supersede_version(current.id)
            await self.chunk_repo.supersede_by_document(current.id)
            await self.dedup_repo.delete_by_content_hash(current.content_hash)
            post.active[current.content_hash] = False

        post.active[content_hash] = True
        # 同一身份在片内出现多次时 fold 到最后一次（A→B→A 只写最终版本）
        post.versions[identity] = (clean_row.id, content_hash)
        seen_hashes[content_hash] = clean_row
        dirty.add(identity)
        return clean_row, created, changed

    async def _apply_identified_committed(
        self, raw: dict, cleaned: dict, *, count_rejected: bool = True
    ) -> tuple[DocumentClean, bool, bool]:
        """
        单篇 / 批量重放用的事务包装器：一个事务、一次 commit。

        冲突分类（200 指向已有文档 / 409 / DocumentInProgress）与 Redis 写后动作
        都在这里完成；批量快路径不走它。

        :param raw: 原始入参 dict。
        :param cleaned: 标准化后的字段 + ``content_hash``。
        :param count_rejected: 重复时是否立即给计数器 +1；批量重放传 ``False``。
        :return: ``(当前版本行, 是否新建, active 集合是否有变动)``。
        :raises ContentConflictError: 更新时新内容已被别的身份持有。
        :raises DocumentInProgressError: 并发创建同一身份，被唯一约束拦下。
        """
        source, external_id = raw["source"], raw["external_id"]
        content_hash = cleaned["content_hash"]
        post = PostCommit()
        state: dict = {}
        try:
            result = await self._apply_identified_in_tx(
                raw,
                cleaned,
                post=post,
                marks_added=set(),
                seen_hashes={},
                dirty=set(),
                count_rejected=count_rejected,
                state=state,
            )
            await self.db.commit()
        except IntegrityError:
            # 【必须 rollback】：不回滚 session 就废了。更新路径上「释放旧内容」
            # 也在同一个事务里，回滚会把它一并撤销 ✓。
            await self.db.rollback()
            await release_mark(self.redis, content_hash)     # 撤掉快路径占位
            existing = await self._find_existing(content_hash)
            if existing is not None:
                if count_rejected:
                    await incr_rejected(self.redis)
                if state.get("conflict_is_409"):
                    logger.info("内容撞车（更新/复活）hash=%s", content_hash[:12])
                    raise ContentConflictError() from None
                logger.info("准入检查（唯一索引兜底）→ 内容已属于别人 hash=%s", content_hash[:12])
                return existing, False, False
            # 并发创建同一身份 → 唯一约束拦下。报 409，让同步器下一轮重推。
            logger.warning("身份并发创建冲突 ext=%s", external_id)
            raise DocumentInProgressError() from None

        # 提交成功之后才写 Redis（DB 是权威；不一致会自愈）
        await self._apply_post_commit(post)
        logger.info(
            "%s 身份路径 ext=%s clean_id=%s",
            "新增" if result[1] else "更新", external_id, result[0].id,
        )
        return result

    async def _apply_post_commit(self, post: PostCommit) -> None:
        """
        提交成功后执行 Redis 副作用：content_hash 按最终活跃状态、docver 按最终版本。

        一批有 10+ 篇时，逐条 await 会产生 2×N 次往返；这里收进一个 pipeline，
        一次 ``execute()`` 发完。仍然只在 commit 之后执行，失败由调用方 best-effort 处理。

        :param post: ``PostCommit`` 收集的净效果（``active`` 与 ``versions``）。
        """
        if not post.active and not post.versions:
            return

        pipe = self.redis.pipeline(transaction=False)
        for content_hash, is_active in post.active.items():
            if is_active:
                pipe.sadd(DEDUP_KEY, content_hash)       # 新内容占位（幂等）
            else:
                pipe.srem(DEDUP_KEY, content_hash)       # 旧内容不再 active
        for (source, external_id), (clean_id, content_hash) in post.versions.items():
            # 与 cache_service.cache_set 保持同一种编码
            pipe.set(
                document_version_key(source, external_id),
                json.dumps(
                    {"clean_id": clean_id, "content_hash": content_hash},
                    ensure_ascii=False,
                ),
                ex=settings.cache_ttl,
            )
        await pipe.execute()

    async def delete_document(self, payload: DocumentDelete) -> bool:
        """
        删除信号（ **幂等** ，返回本次是否真的删掉了）。

        和更新一样 **必须拿同一把身份锁** —— 否则会和 ingest 交错出
        「active 版本还在、``deleted_at`` 却已置上」，那条「deleted ⇒ 没有 active 版本」
        的不变式就破了（读路径靠它才不用 join 到 document_source）。

        一个事务里：作废 active 版本 + 它的块 + **释放它的内容占位** → 置 ``deleted_at`` → bump 列表版本号。

        :param payload: 删除信号（``source`` + ``external_id``）。
        :return: 本次是否真的删掉了；已经删过或从来没有过都返回 ``False``。
        :raises DocumentInProgressError: 同身份正被另一个请求处理。
        """
        lock_id = self._identity_lock_id(payload.source, payload.external_id)
        released_hash: str | None = None      # 被释放占位的那份内容，删完要在 Redis 里也放行
        try:
            async with document_lock(self.redis, lock_id):
                src = await self.repo.get_source(
                    payload.source, payload.external_id, for_update=True
                )
                if src is None or src.deleted_at is not None:
                    await self.db.rollback()        # 没这个身份 / 已经删过 → 幂等返回
                    return False

                current = await self.repo.get_active_version(src.id, for_update=True)
                src.deleted_at = utcnow()
                if current is not None:
                    await self.repo.supersede_version(current.id)
                    await self.chunk_repo.supersede_by_document(current.id)
                    # 文档都没了，它的内容不该继续占着「准入」的位
                    await self.dedup_repo.delete_by_content_hash(current.content_hash)
                    released_hash = current.content_hash
                await self.db.commit()
        except LockNotAcquired:
            logger.info("未拿到锁（同身份正在处理）ext=%s", payload.external_id)
            raise DocumentInProgressError() from None

        if released_hash is not None:
            await release_mark(self.redis, released_hash)
        # 必须把快路径也清掉：否则下次推同样的内容会命中缓存，把"已删除的版本"直接回给调用方
        await cache_delete(
            self.redis, document_version_key(payload.source, payload.external_id)
        )
        await bump_list_version(self.redis)
        logger.info("文档已删除 source=%s ext=%s", payload.source, payload.external_id)
        return True

    async def bulk_ingest(self, payloads: list[DocumentCreate]) -> BulkResult:
        """
        批量摄入（同步器的真实调用方式）。 **与单篇走同一条路** ，只是不拿 Redis 锁。

        事务按 ``settings.bulk_commit_size`` 切片：片内逐篇只 flush、片末 commit 一次。

        失败处理（见 ``_post_commit`` 与片级逻辑）：

        - 片内任何异常 → 整片 rollback → 释放本片新增的 Redis 占位；
        - 瞬时错误（1213/1205）先整片重试（最多 2 次），仍失败再逐篇重放；
        - 其它错误直接逐篇重放 —— 复用单篇路径的分类语义；
        - ``commit`` 成功后本片结果立即定稿；Redis 写后动作失败只记日志，绝不回滚/重放。

        重复 **按条跳过** 、不报错：同步器周期重推是正常行为。

        :param payloads: 本次批量提交的文档（1–200 篇）。
        :return: 汇总结果；``items`` 的顺序与请求一致，逐条状态为
            ``created`` / ``updated`` / ``duplicate`` / ``rejected``。
        """
        total = len(payloads)
        # 按请求下标填结果；最后把 None 过滤掉（除空正文外不会留空位）
        items: list[BulkItemResult | None] = [None] * total

        # ① 先全部标准化（纯函数、便宜），顺带挑出空正文的
        # 标准化后的条目 (请求下标, 原始 dict, 标准化 dict)；空正文已在上面剔除
        prepared: list[tuple[int, dict, dict]] = []
        for i, payload in enumerate(payloads):
            raw = payload.model_dump()
            cleaned = normalize_document(raw)
            if not cleaned["content"]:
                items[i] = BulkItemResult(index=i, status="rejected")
                continue
            prepared.append((i, raw, cleaned))

        created = updated = duplicates = 0
        # 只要尝试过写就 bump 列表版本：提交结果不确定时宁可多失效一次，也别漏
        attempted_write = False
        batch_size = max(1, min(200, settings.bulk_commit_size))

        for start in range(0, len(prepared), batch_size):
            # 当前切片：最多 batch_size 篇
            chunk = prepared[start:start + batch_size]
            # 本片逐条结果；提交成功（或逐篇重放完成）后才并入 items
            results: dict[int, BulkItemResult] = {}
            # 多行写入计划；None 表示这片退回逐篇路径
            planned = None

            if settings.bulk_multirow_enabled:
                planned = await self.planner.plan(
                    chunk, max_chunk_rows=settings.bulk_max_chunk_rows
                )

            if planned is not None:
                # ---- 多行快路径：读 + 分类已完成，这里只负责写与提交 ----
                if planned.writes:
                    attempted_write = True
                results = None
                for attempt in range(3):        # 第一次 + 最多 2 次整片重试
                    try:
                        results, post = await self.planner.write(planned)
                        await self.db.commit()
                    except Exception as exc:
                        await self.db.rollback()
                        if is_transient_error(exc) and attempt < 2:
                            # rollback 之后旧 plan 的行锁与快照都失效了，必须重新读
                            planned = await self.planner.plan(
                                chunk, max_chunk_rows=settings.bulk_max_chunk_rows
                            )
                            if planned is None:
                                break
                            logger.warning(
                                "multirow 分片瞬时错误（%s），重新 plan 后重试 %d/2 start=%d",
                                type(exc).__name__, attempt + 1, start,
                            )
                            continue
                        logger.warning(
                            "multirow 分片失败，回退逐篇 start=%d size=%d",
                            start, len(chunk), exc_info=True,
                        )
                        break
                    else:
                        # 【提交成功即定稿】post-commit 失败只记日志，绝不回滚/重放
                        try:
                            await self._apply_post_commit(post)
                        except Exception:
                            logger.warning(
                                "post-commit Redis 动作失败（DB 已提交）start=%d", start,
                                exc_info=True,
                            )
                        break
                if results is None:
                    results = await self._replay_chunk(chunk)
            else:
                if settings.bulk_multirow_enabled:
                    # plan() 的读阶段可能已经开了事务/拿了行锁 —— 回滚后再走逐篇
                    await self.db.rollback()

                for attempt in range(3):        # 第一次 + 最多 2 次整片重试
                    # 提交成功后才执行的 Redis 副作用
                    post = PostCommit()
                    # 本片 SADD 返回 1 的 hash；片回滚时逐个补偿
                    marks: set[str] = set()
                    # 片内 content_hash -> clean_row，用于同片跨身份判重
                    seen: dict[str, DocumentClean] = {}
                    # 片内已经写过的身份；命中后不再走快路径（缓存还是片前的旧版本）
                    dirty: set[tuple[str, str]] = set()
                    results = {}
                    try:
                        for i, raw, cleaned in chunk:
                            identity = (raw["source"], raw["external_id"])
                            # 片内刚写过这个身份 → 不走快路径（缓存还是片前的旧版本）
                            if identity not in dirty:
                                fast = await self._fast_skip(raw, cleaned, count=False)
                                if fast is not None:
                                    results[i] = BulkItemResult(
                                        index=i, status="duplicate", id=fast.id
                                    )
                                    continue
                            # 调用前就置位：即使调用中途抛异常，也按「尝试过写」bump 一次
                            attempted_write = True
                            clean_row, was_created, changed = await self._apply_identified_in_tx(
                                raw,
                                cleaned,
                                post=post,
                                marks_added=marks,
                                seen_hashes=seen,
                                dirty=dirty,
                                count_rejected=False,
                            )
                            results[i] = self._bulk_item(i, clean_row, was_created, changed)
                        await self.db.commit()       # 只读片也提交：结束事务/快照
                    except Exception as exc:
                        await self.db.rollback()
                        for h in marks:
                            await release_mark(self.redis, h)     # 只补偿本片 SADD=1 的
                        if is_transient_error(exc) and attempt < 2:
                            logger.warning(
                                "bulk 分片瞬时错误（%s），整片重试 %d/2 start=%d size=%d",
                                type(exc).__name__, attempt + 1, start, len(chunk),
                            )
                            continue
                        logger.warning(
                            "bulk 分片回退逐篇 start=%d size=%d err=%s",
                            start, len(chunk), type(exc).__name__,
                        )
                        results = await self._replay_chunk(chunk)
                        break
                    else:
                        # 【提交成功即定稿】post-commit 失败只记日志，绝不回滚/重放
                        try:
                            await self._apply_post_commit(post)
                        except Exception:
                            logger.warning(
                                "post-commit Redis 动作失败（DB 已提交）start=%d", start,
                                exc_info=True,
                            )
                        break

            for i, res in results.items():
                items[i] = res
                if res.status == "created":
                    created += 1
                elif res.status == "updated":
                    updated += 1
                elif res.status == "duplicate":
                    duplicates += 1
            counts = {
                s: sum(1 for r in results.values() if r.status == s)
                for s in ("created", "updated", "duplicate", "rejected")
            }
            logger.info("bulk 分片完成 start=%d size=%d %s", start, len(chunk), counts)

        # 计数器按最终结果加一次（重放/回退不会重复计数）
        await incr_rejected_by(self.redis, duplicates)
        # 只要尝试过写就 bump：提交结果不确定时宁可多失效一次，也别漏
        if created or updated or attempted_write:
            await bump_list_version(self.redis)
        logger.info(
            "bulk_ingest 完成 收到=%d 新建=%d 更新=%d 重复=%d batch=%d",
            total, created, updated, duplicates, batch_size,
        )
        return BulkResult(
            received=total,
            created=created,
            updated=updated,
            duplicates=duplicates,
            rejected=total - created - updated - duplicates,
            items=[it for it in items if it is not None],
        )

    async def _replay_chunk(
        self, chunk: list[tuple[int, dict, dict]]
    ) -> dict[int, BulkItemResult]:
        """
        整片回退后逐篇重放：复用 bulk 的逐篇语义（ **不拿 Redis 身份锁** ），
        每篇一个事务 —— 正确性由已验证的单篇路径保证。

        :param chunk: 要重放的切片，元素为 ``(请求下标, 原始 dict, 标准化 dict)``。
        :return: 请求下标 -> 逐条结果；单篇失败记 ``rejected``，不中断其他篇。
        """
        results: dict[int, BulkItemResult] = {}
        for i, raw, cleaned in chunk:
            fast = await self._fast_skip(raw, cleaned, count=False)
            if fast is not None:
                results[i] = BulkItemResult(index=i, status="duplicate", id=fast.id)
                continue
            try:
                clean_row, was_created, changed = await with_transient_retry(
                    lambda r=raw, c=cleaned: self._apply_identified_committed(
                        r, c, count_rejected=False
                    ),
                    session=self.db,
                )
            except Exception:
                await self.db.rollback()
                logger.exception("批量落库失败 index=%d ext=%s", i, raw.get("external_id"))
                results[i] = BulkItemResult(index=i, status="rejected")
                continue
            results[i] = self._bulk_item(i, clean_row, was_created, changed)
        return results

    @staticmethod
    def _bulk_item(
        i: int, clean_row: DocumentClean, was_created: bool, changed: bool
    ) -> BulkItemResult:
        """
        把身份路径的三元结果翻译成批量响应里的单条状态。

        :param i: 该文档在请求里的下标。
        :param clean_row: 当前版本行（新建、更新或命中的那一版）。
        :param was_created: 是否新建。
        :param changed: active 集合是否发生变动；``False`` 表示这一篇是重复。
        :return: 带 ``created`` / ``updated`` / ``duplicate`` 状态与 id 的单条结果。
        """
        if not changed:
            return BulkItemResult(index=i, status="duplicate", id=clean_row.id)
        return BulkItemResult(
            index=i, status="created" if was_created else "updated", id=clean_row.id
        )
