"""
检索服务：query embedding → Qdrant 向量检索 → MySQL 回表校验。

**MySQL 是真相，Qdrant 只是可重建的索引。** 这里的顺序固定成「先向量召回、再回表」，
原因有两层：

- 正文只存在 MySQL 里（Qdrant payload 不存正文，省空间也避免两份数据不一致）；
- 撤回（文档更新 / 删除）时 Qdrant 的删除是 **异步** 的 —— 召回结果里可能还留着
  「已作废但向量还没删」的点。回表时用 ``superseded_at IS NULL`` 过滤掉它们，
  于是「旧内容在撤回完成前仍然可检索」这个窗口被压缩到一次 SQL 之内。

【模型版本过滤】检索条件里 **强制** 带上 ``embedding_model == 当前模型`` ：
换 embedding 模型重嵌入期间，集合里会同时存在新旧模型的向量，两种向量空间不可比较，
混进来只会让排序莫名其妙。
"""
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import RetrievalUnavailableError
from app.infra.qdrant_client import DENSE_VECTOR_NAME
from app.repositories.chunk_repo import ChunkRepo
from app.schemas.qa import SearchFilters
from app.services.cache_service import cache_get, cache_set

logger = logging.getLogger(__name__)

# query embedding 缓存的键前缀。键里带模型名 —— 换模型后旧向量自然不可达
_EMBED_CACHE_PREFIX = "emb"

# query embedding 缓存最多缓存 10 分钟：即使问题重复，也希望配置微调能尽快生效
_EMBED_CACHE_MAX_TTL = 600


@dataclass
class RetrievalHit:
    """一条检索命中：分数来自 Qdrant，其余字段来自 MySQL 回表。"""

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
    ) -> tuple[list[RetrievalHit], int]:
        """
        检索与问题最相关的若干片段。

        流程：query 向量（带缓存）→ Qdrant ``query_points``（带过滤）→ 按 id 回表
        ``ChunkRepo.get_active_views`` → 按召回顺序组装命中。

        **分数过滤不在这里做**：``RETRIEVAL_SCORE_THRESHOLD`` 的判定留给
        ``AnswerService`` —— 检索接口只负责「返回候选」，问答应不应该拒答是它的决策。

        :param query: 用户问题。
        :param top_k: 最多返回多少条。
        :param filters: 可选的 payload 过滤条件。
        :return: ``(命中列表（分数降序）, 耗时毫秒)``；没有召回时返回 ``([], 耗时)``。
        :raises RetrievalUnavailableError: query embedding 或 Qdrant 调用失败。
        """
        started = time.perf_counter()
        vector = await self._embed_query(query)

        try:
            response = await self.qdrant.query_points(
                collection_name=settings.qdrant_collection,
                query=vector,
                using=DENSE_VECTOR_NAME,
                query_filter=self._build_filter(filters),
                limit=top_k,
                with_payload=True,
            )
        except Exception as exc:  # noqa: BLE001 - 统一翻成 503 给调用方重试
            logger.warning("Qdrant 检索失败：%s", exc)
            raise RetrievalUnavailableError() from exc

        points = list(response.points)
        if not points:
            return [], int((time.perf_counter() - started) * 1000)

        # 保持 Qdrant 的分数顺序；回表只用来补正文与过滤已作废的块
        scores: dict[int, float] = {}
        ordered_ids: list[int] = []
        for point in points:
            chunk_id = int(point.id)
            if chunk_id in scores:
                continue
            scores[chunk_id] = float(point.score)
            ordered_ids.append(chunk_id)

        # MySQL 是真相：删除是异步的，Qdrant 里可能还有已作废的点，回表时一并过滤
        views = await ChunkRepo(self.db).get_active_views(ordered_ids)

        hits = [
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
        # 耗时口径：query embedding + Qdrant 召回 + MySQL 回表（整条读路径）
        return hits, int((time.perf_counter() - started) * 1000)

    async def _embed_query(self, query: str) -> list[float]:
        """
        把问题编码成向量；开启缓存时先查 Redis。

        缓存键带 ``provider.model`` ：换了模型，旧键直接不可达（不必扫描删除）。
        缓存读写失败一律 **吞掉并继续** —— 缓存是加速层，不能成为检索的可用性单点。

        :param query: 用户问题。
        :return: 向量。
        :raises RetrievalUnavailableError: embedding 调用失败。
        """
        cache_key = (
            f"{_EMBED_CACHE_PREFIX}:{self.embedding_model}:"
            f"{hashlib.sha256(query.encode('utf-8')).hexdigest()}"
        )
        use_cache = bool(self.redis) and settings.qa_cache_enabled

        if use_cache:
            try:
                cached = await cache_get(self.redis, cache_key)
            except Exception as exc:  # noqa: BLE001 - 缓存故障不该拖垮检索
                logger.warning("读取 query embedding 缓存失败：%s", exc)
            else:
                if isinstance(cached, list) and cached:
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
