"""
问答编排：检索 → 上下文 / prompt → LLM 流式生成 → SSE 事件。

事件顺序是 **固定契约** （前端按它渲染）：

1. ``citations`` —— 本次回答可引用的资料列表（编号从 1 开始）；
2. ``token`` —— 回答的文本增量（若干条）；
3. ``done`` —— 统计与标记（模型、是否降级、是否有引用、耗时、是否命中缓存）；
4. ``error`` —— 只在中途失败时出现（已经输出的部分保留，不重发）。

【为什么不缓冲整段答案再清洗引用】流式的价值就是「边说边看」；为了剔除几个越界编号
把整段答案攒住再发，等于把流式退回一次性响应，还让首 token 延迟变成总生成时间。
所以这里的取舍是： **已发出的文本不再修改** ，越界编号的数量放进 ``done.invalid_citations``
让前端提示，而不是偷偷换掉用户的屏幕内容。

【降级链】LLM 首 token 之前失败 → 用 ``extractive`` 把检索片段拼成答案（``degraded=true``）；
中途失败 → 保留已输出部分 + ``error`` 事件。任何情况下都不把问答打成 500。
"""
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis

from app.core.config import settings
from app.core.metrics import (
    record_cache_hit,
    record_llm_fallback,
    record_llm_first_token,
    record_llm_tokens,
    record_llm_total,
)
from app.schemas.qa import Citation, SearchFilters
from app.services.cache_service import cache_get, cache_set, get_list_version
from app.services.llm_providers import EXTRACTIVE
from app.services.prompt_builder import (
    REFUSAL_TEXT,
    build_context,
    build_messages,
    validate_citations,
)
from app.services.retrieval_service import RetrievalOutcome, RetrievalService
from app.utils.tokenizer import count_tokens

logger = logging.getLogger(__name__)

# 精确 QA 缓存的键前缀；键里带 prompt 版本与语料版本，任一变化都自然失效
_QA_CACHE_PREFIX = "qa"

# 缓存命中时按这个字符数切块回放：保留流式观感，又不至于把事件切得太碎
_CACHE_CHUNK_SIZE = 24


def _log_qa_request(
    *,
    trace_id: str,
    question_hash: str,
    mode: str,
    top_k: int,
    retrieval_ms: int,
    first_token_ms: int,
    total_ms: float,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    degraded: bool,
    grounded: bool,
    invalid_citations: int,
    cache_hit: bool,
) -> None:
    """
    打一条问答请求的结构化日志（``key=value``，方便 grep 与日志采集侧切分）。

    **不记录问题与答案原文**：只记问题的 ``sha256`` 前 12 位和统计字段 ——
    既避免隐私内容进日志，也让单条日志长度可控。

    :param trace_id: 本次请求的短随机 id（12 位十六进制），用于串联同一次问答的上下游日志。
    :param question_hash: 问题 ``sha256`` 的前 12 位；同题可聚合、不可反推原文。
    :param mode: 实际检索模式（``dense`` / ``hybrid``）；缓存命中时取缓存里记录的模式。
    :param top_k: 请求的检索条数上限。
    :param retrieval_ms: 检索耗时（毫秒）；缓存命中 / 拒答为 0 或实际检索值。
    :param first_token_ms: 首个 token 延迟（毫秒）；没有产出 token 时为 0。
    :param total_ms: 从请求开始到打日志时的总耗时（毫秒）。
    :param model: 实际产出答案的模型（extractive 兜底时为 ``extractive``）。
    :param prompt_tokens: prompt 侧 token 数（估算值）。
    :param completion_tokens: completion 侧 token 数（估算值）。
    :param degraded: 是否降级到 extractive 兜底。
    :param grounded: 回答是否带有效引用。
    :param invalid_citations: 越界引用编号的个数。
    :param cache_hit: 是否命中精确 QA 缓存。
    """
    logger.info(
        "qa_request trace_id=%s question_hash=%s mode=%s top_k=%s retrieval_ms=%s "
        "first_token_ms=%s total_ms=%s model=%s prompt_tokens=%s completion_tokens=%s "
        "degraded=%s grounded=%s invalid_citations=%s cache_hit=%s",
        trace_id,
        question_hash,
        mode,
        top_k,
        retrieval_ms,
        first_token_ms,
        int(total_ms),
        model,
        prompt_tokens,
        completion_tokens,
        degraded,
        grounded,
        invalid_citations,
        cache_hit,
    )


@dataclass
class SseEvent:
    """一个待发送的 SSE 事件（endpoint 层负责序列化）。"""

    event: str
    data: dict[str, Any]


class AnswerService:
    """把「问题」变成「citations / token / done 事件流」。"""

    def __init__(
        self,
        retrieval: RetrievalService,
        llm_provider: Any,
        redis: Redis | None = None,
    ) -> None:
        """
        绑定检索服务、LLM provider 与可选的 Redis。

        :param retrieval: 检索服务（已绑定会话与 embedding provider）。
        :param llm_provider: LLM provider 实例（``openai_compat`` 或 ``extractive``）。
        :param redis: 共享 Redis 客户端；``None`` 表示不启用精确 QA 缓存。
        """
        self.retrieval = retrieval
        self.llm_provider = llm_provider
        self.redis = redis

    async def stream(
        self,
        question: str,
        top_k: int,
        filters: SearchFilters | None,
        *,
        retrieval_mode: str | None = None,
        use_cache: bool = True,
        score_threshold: float | None = None,
    ) -> AsyncIterator[SseEvent]:
        """
        生成一次问答的完整事件流。

        先尝试精确缓存（同问题 + 同过滤 + 同语料版本 + 同 prompt 版本才命中）；
        未命中则检索 → 判拒答 → 拼 prompt → 流式生成。生成成功后写回缓存。

        **拒答阈值统一比较 ``best_dense_score``** ：hybrid 下命中的 ``score`` 是 RRF
        融合分，与余弦分不可比；用 dense 余弦分判断，关键词噪声才绕不过拒答。

        :param question: 用户问题。
        :param top_k: 检索条数上限。
        :param filters: 检索过滤条件。
        :param retrieval_mode: 本次检索模式；``None`` 表示读服务端配置。
        :param use_cache: ``False`` 时既不读也不写精确 QA 缓存（评测要可复现）。
        :param score_threshold: 覆盖服务端拒答阈值；``None`` 时读配置。
        :yield: ``SseEvent`` 序列，顺序见模块 docstring。
        """
        started = time.perf_counter()
        trace_id = uuid.uuid4().hex[:12]
        question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()[:12]
        threshold = (
            score_threshold
            if score_threshold is not None
            else settings.retrieval_score_threshold
        )

        cached = await self._load_cache(
            question,
            filters,
            use_cache=use_cache,
            retrieval_mode=retrieval_mode,
            score_threshold=threshold,
        )
        if cached is not None:
            record_cache_hit("exact")
            cached_usage = cached.get("usage") or {}
            _log_qa_request(
                trace_id=trace_id,
                question_hash=question_hash,
                mode=cached.get("retrieval_mode", "dense"),
                top_k=top_k,
                retrieval_ms=0,
                first_token_ms=0,
                total_ms=(time.perf_counter() - started) * 1000,
                model=cached.get("model", settings.llm_model),
                prompt_tokens=int(cached_usage.get("prompt_tokens", 0)),
                completion_tokens=int(cached_usage.get("completion_tokens", 0)),
                degraded=False,
                grounded=bool(cached.get("grounded", False)),
                invalid_citations=int(cached.get("invalid_citations", 0)),
                cache_hit=True,
            )
            for event in self._replay_cache(cached):
                yield event
            return

        # 检索失败（embedding / Qdrant 不可用）已经流不出 HTTP 503 了（响应头早已发出），
        # 用一个 error 事件告诉前端「这不是空回答，是检索挂了」
        try:
            outcome = await self.retrieval.search(
                question,
                top_k,
                filters,
                mode=retrieval_mode,
                use_cache=use_cache,
            )
        except Exception as exc:  # noqa: BLE001 - 见上：统一转成 error 事件
            logger.warning("问答检索失败：%s", exc)
            yield SseEvent(
                "error",
                {"code": "retrieval_unavailable", "message": str(exc)[:300], "partial": False},
            )
            return

        hits = outcome.hits
        if not hits or (threshold > 0 and outcome.best_dense_score < threshold):
            reason = "no_context" if not hits else "low_score"
            total_ms = (time.perf_counter() - started) * 1000
            _log_qa_request(
                trace_id=trace_id,
                question_hash=question_hash,
                mode=outcome.mode,
                top_k=top_k,
                retrieval_ms=outcome.latency_ms,
                first_token_ms=0,
                total_ms=total_ms,
                model=settings.llm_model,
                prompt_tokens=0,
                completion_tokens=0,
                degraded=False,
                grounded=True,
                invalid_citations=0,
                cache_hit=False,
            )
            for event in self._refusal_events(
                retrieval_ms=outcome.latency_ms,
                total_ms=total_ms,
                reason=reason,
                outcome=outcome,
            ):
                yield event
            return

        context, used_hits = build_context(hits, settings.llm_context_tokens)
        if not used_hits:
            # 预算连一段都放不下（理论上 truncate 会保住一段，这里是防御）
            total_ms = (time.perf_counter() - started) * 1000
            _log_qa_request(
                trace_id=trace_id,
                question_hash=question_hash,
                mode=outcome.mode,
                top_k=top_k,
                retrieval_ms=outcome.latency_ms,
                first_token_ms=0,
                total_ms=total_ms,
                model=settings.llm_model,
                prompt_tokens=0,
                completion_tokens=0,
                degraded=False,
                grounded=True,
                invalid_citations=0,
                cache_hit=False,
            )
            for event in self._refusal_events(
                retrieval_ms=outcome.latency_ms,
                total_ms=total_ms,
                reason="no_context",
                outcome=outcome,
            ):
                yield event
            return

        citations = [
            Citation(
                n=index,
                chunk_id=hit.chunk_id,
                document_id=hit.document_id,
                title=hit.title,
                source=hit.source,
                url=hit.url,
            )
            for index, hit in enumerate(used_hits, start=1)
        ]
        yield SseEvent(
            "citations", {"citations": [citation.model_dump() for citation in citations]}
        )

        messages = build_messages(question, context)
        provider = EXTRACTIVE if settings.llm_provider == "extractive" else self.llm_provider
        # 兜底档没有调用真实模型 —— done.model 要如实写 extractive，不能冒用 LLM_MODEL
        answer_model = "extractive" if provider is EXTRACTIVE else settings.llm_model

        full = ""
        first_token_ms: float | None = None
        degraded = False
        # 只有「首 token 前失败、整段换成 extractive」才算一次降级；
        # 中途失败保留已输出部分的情况不计入 fallback 指标
        fell_back = False

        try:
            async for piece in provider.stream(
                question=question,
                hits=used_hits,
                messages=messages,
                max_tokens=settings.llm_max_tokens,
                temperature=settings.llm_temperature,
            ):
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - started) * 1000
                full += piece
                yield SseEvent("token", {"text": piece})
        except Exception as exc:  # noqa: BLE001 - LLM 故障必须降级，不能 500
            logger.warning("LLM 流式生成失败（已输出 %d 字符）：%s", len(full), exc)
            degraded = True
            if not full and provider is not EXTRACTIVE:
                # 首 token 之前失败：整段换成兜底答案，仍然保持流式
                fell_back = True
                async for piece in EXTRACTIVE.stream(
                    question=question,
                    hits=used_hits,
                    messages=messages,
                    max_tokens=settings.llm_max_tokens,
                    temperature=settings.llm_temperature,
                ):
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000
                    full += piece
                    yield SseEvent("token", {"text": piece})
            elif full:
                # 中途失败：已输出部分保留，不追加任何内容，只发 error 事件
                yield SseEvent(
                    "error",
                    {
                        "code": "llm_stream_failed",
                        "message": str(exc)[:300],
                        "partial": True,
                    },
                )

        total_ms = (time.perf_counter() - started) * 1000

        # 已流出去的文本不改；只统计越界编号的数量（清理后的文本此处用不上）
        _, valid_ids, invalid_ids = validate_citations(full, len(citations))
        grounded = bool(valid_ids) and not full.strip().startswith(REFUSAL_TEXT)

        prompt_tokens = count_tokens(context)
        completion_tokens = count_tokens(full)

        # 指标只做可观测：降级 / 中途失败也照常记录，用 done.degraded 区分
        record_llm_total(answer_model, total_ms / 1000)
        if first_token_ms is not None:
            record_llm_first_token(answer_model, first_token_ms / 1000)
        record_llm_tokens(prompt_tokens, completion_tokens)
        if fell_back:
            record_llm_fallback("llm_failed")

        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated": True,
        }
        latency = {
            "retrieval": outcome.latency_ms,
            "first_token": int(first_token_ms) if first_token_ms is not None else 0,
            "total": int(total_ms),
        }

        score_type = "rrf" if outcome.mode == "hybrid" else "cosine"
        done: dict[str, Any] = {
            "model": answer_model,
            "degraded": degraded,
            "grounded": grounded,
            "invalid_citations": len(invalid_ids),
            "usage": usage,
            "latency_ms": latency,
            "cache_hit": False,
            "retrieval_mode": outcome.mode,
            "score_type": score_type,
            "best_dense_score": outcome.best_dense_score,
            "keyword_hits": outcome.keyword_hits,
        }
        if degraded:
            done["reason"] = "llm_failed"
        _log_qa_request(
            trace_id=trace_id,
            question_hash=question_hash,
            mode=outcome.mode,
            top_k=top_k,
            retrieval_ms=outcome.latency_ms,
            first_token_ms=int(first_token_ms) if first_token_ms is not None else 0,
            total_ms=total_ms,
            model=answer_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            degraded=degraded,
            grounded=grounded,
            invalid_citations=len(invalid_ids),
            cache_hit=False,
        )
        yield SseEvent("done", done)

        # 只有正常完成才写缓存：降级 / 中断的回答不该被当成标准答案回放
        if not degraded and full.strip():
            await self._store_cache(
                question=question,
                filters=filters,
                answer=full,
                citations=citations,
                grounded=grounded,
                invalid_citations=len(invalid_ids),
                usage=usage,
                model=answer_model,
                use_cache=use_cache,
                retrieval_mode=outcome.mode,
                score_type=score_type,
                best_dense_score=outcome.best_dense_score,
                keyword_hits=outcome.keyword_hits,
                score_threshold=threshold,
            )

    # ---------- 缓存 ----------

    @staticmethod
    def _resolved_mode(retrieval_mode: str | None) -> Literal["dense", "hybrid"]:
        """
        把请求里的可选模式归一成实际模式。

        :param retrieval_mode: 请求覆盖值；``None`` / 非法值都回落到服务端配置。
        :return: ``dense`` 或 ``hybrid``。
        """
        mode = retrieval_mode or settings.retrieval_mode
        return "hybrid" if mode == "hybrid" else "dense"

    async def _load_cache(
        self,
        question: str,
        filters: SearchFilters | None,
        *,
        use_cache: bool = True,
        retrieval_mode: str | None = None,
        score_threshold: float | None = None,
    ) -> dict[str, Any] | None:
        """
        读精确 QA 缓存。

        键包含：prompt 版本、模型名、语料版本（``documents:list:version``）、过滤条件摘要、
        检索模式、拒答阈值与问题摘要。语料版本来自 Redis 的列表版本号 —— 文档一写就变，
        旧答案自然不可达。模式与阈值也参与键，避免「dense 的答案回放给 hybrid 请求」，
        以及「低阈值下生成的答案被高阈值请求直接复用」。

        :param question: 用户问题。
        :param filters: 过滤条件（参与键计算）。
        :param use_cache: ``False`` 时直接不读（评测要求可复现）。
        :param retrieval_mode: 本次检索模式（参与键计算）。
        :param score_threshold: 本次拒答阈值（参与键计算）。
        :return: 缓存内容；未命中 / 未启用 / 关闭缓存 / Redis 异常时返回 ``None``。
        """
        if not use_cache or not self.redis or not settings.qa_cache_enabled:
            return None

        try:
            corpus_version = await get_list_version(self.redis)
        except Exception as exc:  # noqa: BLE001 - 版本取不到就按 0 算，仍可能命中
            logger.warning("读取语料版本失败，按 0 处理：%s", exc)
            corpus_version = 0

        try:
            return await cache_get(
                self.redis,
                self._cache_key(
                    question,
                    filters,
                    corpus_version,
                    self._resolved_mode(retrieval_mode),
                    score_threshold,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 缓存故障直接走正常路径
            logger.warning("读取 QA 缓存失败：%s", exc)
            return None

    async def _store_cache(
        self,
        *,
        question: str,
        filters: SearchFilters | None,
        answer: str,
        citations: list[Citation],
        grounded: bool,
        invalid_citations: int,
        usage: dict[str, Any],
        model: str,
        use_cache: bool = True,
        retrieval_mode: str = "dense",
        score_type: str = "cosine",
        best_dense_score: float = 0.0,
        keyword_hits: int = 0,
        score_threshold: float | None = None,
    ) -> None:
        """
        写精确 QA 缓存（best-effort，异常只告警）。

        ``done`` 里的检索诊断字段一并存下，回放时才能如实告诉调用方
        「这条答案是哪条检索路径产出的」。

        :param question: 用户问题。
        :param filters: 过滤条件（参与键计算）。
        :param answer: 完整回答。
        :param citations: 本次提供的引用。
        :param grounded: 是否有有效引用。
        :param invalid_citations: 越界引用数量。
        :param usage: token 用量。
        :param model: 实际产出答案的模型（extractive 兜底时不是 LLM_MODEL）。
        :param use_cache: ``False`` 时直接不写（评测要求可复现）。
        :param retrieval_mode: 本次实际检索模式。
        :param score_type: ``cosine`` / ``rrf``。
        :param best_dense_score: dense 候选第一条的余弦分。
        :param keyword_hits: 关键词一路返回条数。
        :param score_threshold: 本次拒答阈值（参与键计算）。
        """
        if not use_cache or not self.redis or not settings.qa_cache_enabled:
            return
        try:
            corpus_version = await get_list_version(self.redis)
            payload = {
                "answer": answer,
                "citations": [citation.model_dump() for citation in citations],
                "grounded": grounded,
                "invalid_citations": invalid_citations,
                "usage": usage,
                "model": model,
                "retrieval_mode": retrieval_mode,
                "score_type": score_type,
                "best_dense_score": best_dense_score,
                "keyword_hits": keyword_hits,
            }
            await cache_set(
                self.redis,
                self._cache_key(
                    question,
                    filters,
                    corpus_version,
                    self._resolved_mode(retrieval_mode),
                    score_threshold,
                ),
                payload,
                ttl=settings.qa_cache_ttl,
            )
        except Exception as exc:  # noqa: BLE001 - 写缓存失败不影响本次响应
            logger.warning("写入 QA 缓存失败：%s", exc)

    def _cache_key(
        self,
        question: str,
        filters: SearchFilters | None,
        corpus_version: int,
        retrieval_mode: str = "dense",
        score_threshold: float | None = None,
    ) -> str:
        """
        拼精确 QA 缓存键。

        过滤条件先序列化成稳定 JSON（``sort_keys``）再摘要，避免「字段顺序不同导致键不同」
        这种脏 miss；整体 key 里同时带版本、模型、检索模式与阈值，任一变化都让旧键不可达。

        :param question: 用户问题。
        :param filters: 过滤条件。
        :param corpus_version: 语料版本号。
        :param retrieval_mode: 检索模式。
        :param score_threshold: 拒答阈值；``None`` 记成 ``default``。
        :return: 缓存键。
        """
        filters_json = json.dumps(
            filters.model_dump() if filters else {},
            sort_keys=True,
            ensure_ascii=False,
        )
        filters_hash = hashlib.sha256(filters_json.encode("utf-8")).hexdigest()[:16]
        question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
        threshold_key = (
            "default" if score_threshold is None else f"{score_threshold:.6f}"
        )
        return (
            f"{_QA_CACHE_PREFIX}:v{settings.prompt_version}:{settings.llm_model}:"
            f"{corpus_version}:{filters_hash}:{retrieval_mode}:{threshold_key}:"
            f"{question_hash}"
        )

    def _replay_cache(self, cached: dict[str, Any]) -> list[SseEvent]:
        """
        把缓存内容回放成事件流（耗时全部记 0，``cache_hit=True``）。

        检索诊断字段从缓存里取；旧缓存没有这些字段时按 dense 处理 —— 它们都是
        在 hybrid 上线之前写入的，当时的答案确实来自 dense 检索。

        :param cached: ``_store_cache`` 写入的结构。
        :return: citations / token / done 三类事件。
        """
        events: list[SseEvent] = [
            SseEvent("citations", {"citations": cached.get("citations", [])})
        ]
        answer = cached.get("answer", "")
        for start in range(0, len(answer), _CACHE_CHUNK_SIZE):
            events.append(
                SseEvent("token", {"text": answer[start:start + _CACHE_CHUNK_SIZE]})
            )
        retrieval_mode = cached.get("retrieval_mode", "dense")
        events.append(
            SseEvent(
                "done",
                {
                    "model": cached.get("model", settings.llm_model),
                    "degraded": False,
                    "grounded": cached.get("grounded", False),
                    "invalid_citations": cached.get("invalid_citations", 0),
                    "usage": cached.get("usage", {}),
                    "latency_ms": {"retrieval": 0, "first_token": 0, "total": 0},
                    "cache_hit": True,
                    "retrieval_mode": retrieval_mode,
                    "score_type": cached.get(
                        "score_type",
                        "rrf" if retrieval_mode == "hybrid" else "cosine",
                    ),
                    "best_dense_score": cached.get("best_dense_score", 0.0),
                    "keyword_hits": cached.get("keyword_hits", 0),
                },
            )
        )
        return events

    def _refusal_events(
        self,
        *,
        retrieval_ms: int,
        total_ms: float,
        reason: str,
        outcome: RetrievalOutcome,
    ) -> list[SseEvent]:
        """
        构造「不调 LLM 的拒答」事件流。

        :param retrieval_ms: 检索耗时（毫秒）。
        :param total_ms: 从请求开始到现在的耗时（毫秒）。
        :param reason: ``no_context``（没有召回）或 ``low_score``（分数低于阈值）。
        :param outcome: 检索结果（用于回填模式与诊断字段）。
        :return: citations（空）/ token（固定拒答）/ done 三类事件。
        """
        return [
            SseEvent("citations", {"citations": []}),
            SseEvent("token", {"text": REFUSAL_TEXT}),
            SseEvent(
                "done",
                {
                    "model": settings.llm_model,
                    "degraded": False,
                    "grounded": True,
                    "invalid_citations": 0,
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                        "estimated": True,
                    },
                    "latency_ms": {
                        "retrieval": retrieval_ms,
                        "first_token": 0,
                        "total": int(total_ms),
                    },
                    "cache_hit": False,
                    "reason": reason,
                    "retrieval_mode": outcome.mode,
                    "score_type": "rrf" if outcome.mode == "hybrid" else "cosine",
                    "best_dense_score": outcome.best_dense_score,
                    "keyword_hits": outcome.keyword_hits,
                },
            ),
        ]
