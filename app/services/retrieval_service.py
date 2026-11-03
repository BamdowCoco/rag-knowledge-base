"""
检索服务：query embedding → 向量 / 关键词召回 → MySQL 回表校验。

**MySQL 是真相，Qdrant 只是可重建的索引。** 这里的顺序固定成「先召回、再回表」，
原因有两层：

- 正文只存在 MySQL 里（Qdrant payload 不存正文，省空间也避免两份数据不一致）；
- 撤回（文档更新 / 删除）时 Qdrant 的删除是 **异步** 的 —— 召回结果里可能还留着
  「已作废但向量还没删」的点。回表时用 ``superseded_at IS NULL`` 过滤掉它们，
  于是「旧内容在撤回完成前仍然可检索」这个窗口被压缩到一次 SQL 之内。

【两种检索模式】

- ``dense``：只用向量检索，分数是余弦相似度；
- ``hybrid``：向量 + MySQL FULLTEXT（ngram）各取一批候选，用 RRF 按排名融合
  （见 ``app.services.fusion``）。关键词一路失败（索引缺失、SQL 异常）时只记
  warning 并退回 dense —— 检索增强不能变成可用性单点。

【模型版本过滤】两种模式都 **强制** 带上 ``embedding_model == 当前模型`` ：
换 embedding 模型重嵌入期间，集合里会同时存在新旧模型的向量，两种向量空间不可比较，
混进来只会让排序莫名其妙。
"""
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from qdrant_client import AsyncQdrantClient, models
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import RetrievalUnavailableError
from app.core.metrics import observe_retrieval, record_cache_hit
from app.infra.qdrant_client import DENSE_VECTOR_NAME
from app.repositories.chunk_repo import ChunkRepo, ChunkView
from app.schemas.qa import SearchFilters
from app.services.cache_service import cache_get, cache_set
from app.services.fusion import rrf_fuse

logger = logging.getLogger(__name__)

# query embedding 缓存的键前缀。键里带模型名 —— 换模型后旧向量自然不可达
_EMBED_CACHE_PREFIX = "emb"

# query embedding 缓存最多缓存 10 分钟：即使问题重复，也希望配置微调能尽快生效
_EMBED_CACHE_MAX_TTL = 600


@dataclass
class RetrievalHit:
    """
    一条检索命中：正文与元数据来自 MySQL 回表。

    ``score`` 的含义取决于模式：dense 下是余弦相似度；hybrid 下是 RRF 融合分。
    两者不可跨模式比较，所以拒绝判断统一看 ``RetrievalOutcome.best_dense_score``。
    """

    chunk_id: int
    document_id: int
    title: str
    source: str
    url: str | None
    content: str
    score: float
    chunk_index: int
    language: str | None
    tags: list[str] | None


@dataclass
class RetrievalOutcome:
    """
    一次检索的完整结果（不只是 hits）。

    :param hits: 回表后仍 active 的命中，按最终分数降序。
    :param latency_ms: 整条读路径耗时（embedding + 召回 + 回表）。
    :param mode: 实际走的模式；hybrid 的关键词一路失败时会退回 ``dense``。
    :param best_dense_score: dense 候选第一条的余弦分；拒答阈值统一比较它
        （RRF 分与余弦分不可比，不能让关键词噪声绕过拒答）。
    :param keyword_hits: 关键词一路返回的条数（dense 模式恒为 0）。
    """

    hits: list[RetrievalHit]
    latency_ms: int
    mode: Literal["dense", "hybrid"]
    best_dense_score: float
    keyword_hits: int


class RetrievalService:
    """把「问题」变成「带正文与元数据的命中片段列表」。"""

    def __init__(
        self,
        db: AsyncSession,
        qdrant: AsyncQdrantClient,
        embedding_provider: Any,
        redis: Redis | None = None,
    ) -> None:
        """
        绑定请求级会话与共享客户端。

        只取 provider 的 ``model`` / ``dim`` / ``embed`` —— 不依赖具体实现，
        测试里换成 ``hash`` 桩即可（见 ``app.services.embedding_providers``）。

        :param db: 请求级异步会话（回表用）。
        :param qdrant: 共享的异步 Qdrant 客户端。
        :param embedding_provider: embedding provider 实例。
        :param redis: 共享 Redis 客户端；``None`` 表示不做 query embedding 缓存。
        """
        self.db = db
        self.qdrant = qdrant
        self.embedding_provider = embedding_provider
        self.redis = redis
        self.embedding_model: str = embedding_provider.model
        self.embedding_dim: int = embedding_provider.dim

    async def search(
        self,
        query: str,
        top_k: int,
        filters: SearchFilters | None,
        *,
        mode: str | None = None,
        use_cache: bool = True,
    ) -> RetrievalOutcome:
        """
        检索与问题最相关的若干片段。

        dense 流程：query 向量（带缓存）→ Qdrant ``query_points``（带过滤）→ 按 id
        回表 ``ChunkRepo.get_active_views`` → 按召回顺序组装命中。

        hybrid 流程：dense 取 ``RETRIEVAL_CANDIDATE_K``、关键词取同量候选 →
        ``rrf_fuse`` 融合取 ``top_k`` → 回表一次。关键词一路异常时退回 dense。

        **分数阈值不在这里判**：``RETRIEVAL_SCORE_THRESHOLD`` 的判定留给
        ``AnswerService`` —— 检索接口只负责「返回候选」，问答应不应该拒答是它的决策。

        :param query: 用户问题。
        :param top_k: 最多返回多少条。
        :param filters: 可选的 payload / SQL 过滤条件。
        :param mode: 本次检索模式；``None`` 表示读 ``settings.retrieval_mode``。
        :param use_cache: ``False`` 时不读写 query embedding 缓存（评测要可复现）。
        :return: 命中列表、耗时、实际模式与诊断字段。
        :raises RetrievalUnavailableError: query embedding 或 Qdrant 调用失败。
        """
        started = time.perf_counter()
        requested = mode or settings.retrieval_mode
        resolved: Literal["dense", "hybrid"] = (
            "hybrid" if requested == "hybrid" else "dense"
        )

        vector = await self._embed_query(query, use_cache=use_cache)

        if resolved == "dense":
            dense_ids, dense_scores = await self._dense_candidates(vector, top_k, filters)
            views = await ChunkRepo(self.db).get_active_views(dense_ids)
            hits = self._build_hits(views, dense_scores)
            return self._observed(
                RetrievalOutcome(
                    hits=hits,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    mode="dense",
                    # 回表后第一条 active 命中的分数；没有命中则为 0
                    best_dense_score=hits[0].score if hits else 0.0,
                    keyword_hits=0,
                )
            )

        # hybrid：候选数至少覆盖 top_k（配置调小时也不会返回不足条数）
        candidate_k = max(settings.retrieval_candidate_k, top_k)
        dense_ids, dense_scores = await self._dense_candidates(vector, candidate_k, filters)
        # 拒答阈值以 dense 原始候选的第一条为准（RRF 分不可比）
        best_dense_score = dense_scores[dense_ids[0]] if dense_ids else 0.0

        try:
            keyword_pairs = await ChunkRepo(self.db).keyword_search(
                query,
                candidate_k,
                embedding_model=self.embedding_model,
                source=filters.source if filters else None,
                language=filters.language if filters else None,
                tags=filters.tags if filters else None,
                document_ids=filters.document_ids if filters else None,
            )
        except Exception as exc:  # noqa: BLE001 - 索引缺失等：退回 dense，不打挂检索
            logger.warning("关键词检索失败，本次退回 dense：%s", exc)
            views = await ChunkRepo(self.db).get_active_views(dense_ids[:top_k])
            hits = self._build_hits(views, dense_scores)
            return self._observed(
                RetrievalOutcome(
                    hits=hits,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    mode="dense",
                    best_dense_score=hits[0].score if hits else 0.0,
                    keyword_hits=0,
                )
            )

        keyword_ids = [chunk_id for chunk_id, _ in keyword_pairs]
        fused = rrf_fuse([dense_ids, keyword_ids], k=settings.rrf_k, limit=top_k)
        fused_scores = dict(fused)
        views = await ChunkRepo(self.db).get_active_views(
            [chunk_id for chunk_id, _ in fused]
        )
        return self._observed(
            RetrievalOutcome(
                hits=self._build_hits(views, fused_scores),
                latency_ms=int((time.perf_counter() - started) * 1000),
                mode="hybrid",
                best_dense_score=best_dense_score,
                keyword_hits=len(keyword_ids),
            )
        )

    @staticmethod
    def _observed(outcome: RetrievalOutcome) -> RetrievalOutcome:
        """
        记录本次检索的指标后原样返回结果。

        埋点只做可观测：即使耗时或条数为 0 也照常记录，不影响调用方拿到的数据。

        :param outcome: 检索结果。
        :return: 传入的同一个 ``RetrievalOutcome``。
        """
        observe_retrieval(
            outcome.mode, outcome.latency_ms / 1000, len(outcome.hits)
        )
        return outcome

    async def _dense_candidates(
        self,
        vector: list[float],
        limit: int,
        filters: SearchFilters | None,
    ) -> tuple[list[int], dict[int, float]]:
        """
        向量召回，返回去重后的 id 顺序与 id → 分数映射。

        :param vector: query 向量。
        :param limit: Qdrant 返回条数上限。
        :param filters: 业务过滤条件。
        :return: ``(按分数降序的 chunk id, id → 余弦分)``。
        :raises RetrievalUnavailableError: Qdrant 不可用。
        """
        try:
            response = await self.qdrant.query_points(
                collection_name=settings.qdrant_collection,
                query=vector,
                using=DENSE_VECTOR_NAME,
                query_filter=self._build_filter(filters),
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001 - 统一翻成 503 给调用方重试
            logger.warning("Qdrant 检索失败：%s", exc)
            raise RetrievalUnavailableError() from exc

        ordered_ids: list[int] = []
        scores: dict[int, float] = {}
        for point in response.points:
            chunk_id = int(point.id)
            if chunk_id in scores:
                continue
            scores[chunk_id] = float(point.score)
            ordered_ids.append(chunk_id)
        return ordered_ids, scores

    def _build_hits(
        self, views: list[ChunkView], scores: dict[int, float]
    ) -> list[RetrievalHit]:
        """
        把回表结果与分数拼成命中列表，保持 ``views`` 的顺序。

        :param views: 已过滤 active 的块（顺序与召回 / 融合顺序一致）。
        :param scores: chunk id → 分数（余弦或 RRF）。
        :return: 命中列表。
        """
        return [
            RetrievalHit(
                chunk_id=view.id,
                document_id=view.document_id,
                title=view.title,
                source=view.source,
                url=view.url,
                content=view.content,
                score=scores[view.id],
                chunk_index=view.chunk_index,
                language=view.language,
                tags=view.tags,
            )
            for view in views
        ]

    async def _embed_query(self, query: str, *, use_cache: bool = True) -> list[float]:
        """
        把问题编码成向量；开启缓存时先查 Redis。

        缓存键带 ``provider.model`` ：换了模型，旧键直接不可达（不必扫描删除）。
        缓存读写失败一律 **吞掉并继续** —— 缓存是加速层，不能成为检索的可用性单点。

        :param query: 用户问题。
        :param use_cache: ``False`` 时既不读也不写缓存（评测要求结果可复现）。
        :return: 向量。
        :raises RetrievalUnavailableError: embedding 调用失败。
        """
        cache_key = (
            f"{_EMBED_CACHE_PREFIX}:{self.embedding_model}:"
            f"{hashlib.sha256(query.encode('utf-8')).hexdigest()}"
        )
        use_cache = use_cache and bool(self.redis) and settings.qa_cache_enabled

        if use_cache:
            try:
                cached = await cache_get(self.redis, cache_key)
            except Exception as exc:  # noqa: BLE001 - 缓存故障不该拖垮检索
                logger.warning("读取 query embedding 缓存失败：%s", exc)
            else:
                if isinstance(cached, list) and cached:
                    # 只有真正拿到可用向量才算命中；读到空值仍走重新编码
                    record_cache_hit("embedding")
                    return cached

        try:
            vectors = await self.embedding_provider.embed([query])
        except Exception as exc:  # noqa: BLE001 - embedding 上游不可用 → 503
            logger.warning("query embedding 失败：%s", exc)
            raise RetrievalUnavailableError() from exc

        vector = vectors[0]
        if use_cache:
            try:
                await cache_set(
                    self.redis,
                    cache_key,
                    vector,
                    ttl=min(settings.qa_cache_ttl, _EMBED_CACHE_MAX_TTL),
                )
            except Exception as exc:  # noqa: BLE001 - 写缓存失败不影响本次结果
                logger.warning("写入 query embedding 缓存失败：%s", exc)
        return vector

    def _build_filter(self, filters: SearchFilters | None) -> models.Filter:
        """
        把业务过滤条件翻成 Qdrant 的 ``Filter``。

        无论调用方传什么，**都必须** 带上 ``embedding_model`` 条件（见模块 docstring）。

        :param filters: 业务过滤条件；``None`` 表示只按模型版本过滤。
        :return: Qdrant 过滤器。
        """
        must: list[Any] = [
            models.FieldCondition(
                key="embedding_model",
                match=models.MatchValue(value=self.embedding_model),
            )
        ]
        if filters is None:
            return models.Filter(must=must)

        if filters.source:
            must.append(
                models.FieldCondition(
                    key="source", match=models.MatchAny(any=filters.source)
                )
            )
        if filters.language:
            must.append(
                models.FieldCondition(
                    key="language", match=models.MatchValue(value=filters.language)
                )
            )
        if filters.tags:
            must.append(
                models.FieldCondition(
                    key="tags", match=models.MatchAny(any=filters.tags)
                )
            )
        if filters.document_ids:
            must.append(
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchAny(any=list(filters.document_ids)),
                )
            )
        return models.Filter(must=must)
