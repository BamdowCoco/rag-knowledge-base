"""
检索与问答接口（ ``APP_ROLE`` 为 ``rag`` / ``all`` 时才挂载）。

三条路由：

- ``POST /search``       ：向量检索，返回片段与分数；
- ``POST /chat/stream``  ：带引用的流式问答（SSE）；
- ``POST /feedback``     ：用户反馈，只做追加写入（best-effort）。

SSE 事件顺序：``citations`` → ``token``（若干）→ ``done``；中途失败时插入 ``error``。
前端按事件名分支即可，不必自己拼半截 JSON —— 事件边界由 ``format_sse`` 的空行保证。
"""
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.api.deps import get_answer_service, get_retrieval_service
from app.infra.redis_client import get_redis
from app.schemas.qa import (
    FeedbackRequest,
    FeedbackResult,
    RetrievalResult,
    SearchFilters,
    SearchRequest,
    SearchResponse,
    ChatRequest,
)
from app.services.answer_service import AnswerService
from app.services.retrieval_service import RetrievalService
from app.utils.sse import format_sse

logger = logging.getLogger(__name__)

router = APIRouter()

# 反馈列表最多保留的条数：只需要近期样本，不能让一个开放接口把 Redis 写满
_FEEDBACK_KEY = "qa:feedback"
_FEEDBACK_MAX_ITEMS = 1000

# 公网访客区的固定问答范围。Caddy 在 /demo/* 反代时注入 ``X-Demo-Mode: 1``，
# 前端也把筛选项锁死；这里再兜底一次，防止有人直接拿访客账号调接口改范围。
#
# 只锁 source 与 top_k：``source=web`` 就是 FastAPI 中文文档，语言由语料保证。
# 不再叠加 ``language=zh`` —— enrichment 的语言字段晚于向量写入，历史 chunk 的
# Qdrant payload 可能是 null，硬过滤会把正常文档排除掉。
DEMO_HEADER = "x-demo-mode"
DEMO_SOURCE = "web"
DEMO_TOP_K = 5


def _demo_scope(
    demo: bool,
    top_k: int,
    filters: SearchFilters | None,
) -> tuple[int, SearchFilters | None]:
    """
    按访客模式收窄检索范围。

    :param demo: 请求是否来自访客区（由 Caddy 注入的请求头判定）。
    :param top_k: 调用方请求的召回数。
    :param filters: 调用方请求的过滤条件。
    :return: ``(top_k, filters)``；非访客模式原样返回。
    """
    if not demo:
        return top_k, filters
    narrowed = filters.model_copy() if filters is not None else SearchFilters()
    narrowed = narrowed.model_copy(
        update={"source": [DEMO_SOURCE], "language": None}
    )
    return DEMO_TOP_K, narrowed


@router.post("/search", response_model=SearchResponse, summary="向量 / 混合检索")
async def search_chunks(
    payload: SearchRequest,
    request: Request,
    service: RetrievalService = Depends(get_retrieval_service),
) -> SearchResponse:
    """
    按语义（dense）或语义 + 关键词（hybrid，RRF 融合）检索分块。

    ``payload.score_threshold`` **在这里不生效** ：检索接口只返回候选，
    拒答阈值由 ``/chat/stream`` 使用（``AnswerService`` 统一比较 ``best_dense_score``）。
    字段保留是为了让两个接口的请求体形状一致，评测脚本不必按路由区别对待。

    访客区请求（``X-Demo-Mode: 1``）会被强制收窄为 ``source=web``、``top_k=5``；
    ``source=web`` 即 FastAPI 中文文档，语言由语料保证。

    :param payload: 查询文本、条数上限、过滤条件与按次覆盖项。
    :param request: 用于读取 Caddy 注入的访客标记。
    :param service: 检索服务。
    :return: 命中片段（最终分数降序）、耗时、embedding 模型与检索诊断字段。
    :raises RetrievalUnavailableError: Qdrant / embedding 不可用（映射为 503）。
    """
    top_k, filters = _demo_scope(
        request.headers.get(DEMO_HEADER) == "1", payload.top_k, payload.filters
    )
    outcome = await service.search(
        payload.query,
        top_k,
        filters,
        mode=payload.retrieval_mode,
        use_cache=payload.use_cache,
    )
    return SearchResponse(
        results=[
            RetrievalResult(
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                title=hit.title,
                source=hit.source,
                url=hit.url,
                content=hit.content,
                score=hit.score,
                chunk_index=hit.chunk_index,
                language=hit.language,
                tags=hit.tags,
            )
            for hit in outcome.hits
        ],
        latency_ms=outcome.latency_ms,
        embedding_model=service.embedding_model,
        retrieval_mode=outcome.mode,
        score_type="rrf" if outcome.mode == "hybrid" else "cosine",
        best_dense_score=outcome.best_dense_score,
        keyword_hits=outcome.keyword_hits,
    )


@router.post("/chat/stream", summary="流式问答（SSE）")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    service: AnswerService = Depends(get_answer_service),
) -> StreamingResponse:
    """
    流式问答。

    事件顺序：

    - ``citations``：可引用的资料列表 ``{"citations": [...]}``；
    - ``token``：回答文本增量 ``{"text": "..."}``；
    - ``done``：统计与标记（``model`` / ``degraded`` / ``grounded`` / ``cache_hit`` /
      ``retrieval_mode`` / ``score_type`` / ``best_dense_score`` / ``keyword_hits`` 等）；
    - ``error``：只在检索失败或生成中途失败时出现。

    无检索结果时不会调用 LLM，直接返回固定拒答并把 ``done.reason`` 置为 ``no_context``。

    访客区请求（``X-Demo-Mode: 1``）会被强制收窄为 ``source=web``、``top_k=5``；
    ``source=web`` 即 FastAPI 中文文档，语言由语料保证。

    :param payload: 问题、条数上限、过滤条件与按次覆盖项。
    :param request: 用于读取 Caddy 注入的访客标记。
    :param service: 问答服务。
    :return: ``text/event-stream`` 流式响应。
    """
    top_k, filters = _demo_scope(
        request.headers.get(DEMO_HEADER) == "1", payload.top_k, payload.filters
    )

    async def gen() -> AsyncIterator[str]:
        """
        把服务层的事件逐个序列化成 SSE 文本。

        :yield: 以空行结尾的 SSE 消息。
        """
        async for event in service.stream(
            payload.question,
            top_k,
            filters,
            retrieval_mode=payload.retrieval_mode,
            use_cache=payload.use_cache,
            score_threshold=payload.score_threshold,
        ):
            yield format_sse(event.event, event.data)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            # 关掉中间层缓冲：nginx 不攒流，浏览器 / curl 才能实时看到 token
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/feedback", response_model=FeedbackResult, summary="问答反馈")
async def submit_feedback(
    payload: FeedbackRequest,
    redis: Redis = Depends(get_redis),
) -> FeedbackResult:
    """
    记录用户对回答的评价。

    **best-effort**：写失败只记日志，仍然返回成功 —— 反馈是锦上添花，
    不该让用户因为 Redis 抖动看到报错。

    :param payload: 评分与可选评论。
    :param redis: 共享 Redis 客户端。
    :return: ``{"ok": true}``。
    """
    try:
        await redis.lpush(
            _FEEDBACK_KEY, json.dumps(payload.model_dump(), ensure_ascii=False)
        )
        await redis.ltrim(_FEEDBACK_KEY, 0, _FEEDBACK_MAX_ITEMS - 1)
    except Exception as exc:  # noqa: BLE001 - 见上：反馈写失败不影响调用方
        logger.warning("写入反馈失败：%s", exc)
    return FeedbackResult(ok=True)
