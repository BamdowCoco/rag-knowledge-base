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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings
from app.schemas.qa import Citation, SearchFilters
from app.services.cache_service import cache_get, cache_set, get_list_version
from app.services.llm_providers import EXTRACTIVE
from app.services.prompt_builder import (
    REFUSAL_TEXT,
    build_context,
    build_messages,
    validate_citations,
)
from app.services.retrieval_service import RetrievalService
from app.utils.tokenizer import count_tokens

logger = logging.getLogger(__name__)

# 精确 QA 缓存的键前缀；键里带 prompt 版本与语料版本，任一变化都自然失效
_QA_CACHE_PREFIX = "qa"

# 缓存命中时按这个字符数切块回放：保留流式观感，又不至于把事件切得太碎
_CACHE_CHUNK_SIZE = 24


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
    ) -> AsyncIterator[SseEvent]:
        """
        生成一次问答的完整事件流。

        先尝试精确缓存（同问题 + 同过滤 + 同语料版本 + 同 prompt 版本才命中）；
        未命中则检索 → 判拒答 → 拼 prompt → 流式生成。生成成功后写回缓存。

        :param question: 用户问题。
        :param top_k: 检索条数上限。
        :param filters: 检索过滤条件。
        :yield: ``SseEvent`` 序列，顺序见模块 docstring。
        """
        started = time.perf_counter()

        cached = await self._load_cache(question, filters)
        if cached is not None:
            for event in self._replay_cache(cached):
                yield event
            return

        # 检索失败（embedding / Qdrant 不可用）已经流不出 HTTP 503 了（响应头早已发出），
        # 用一个 error 事件告诉前端「这不是空回答，是检索挂了」
        try:
            hits, retrieval_ms = await self.retrieval.search(question, top_k, filters)
        except Exception as exc:  # noqa: BLE001 - 见上：统一转成 error 事件
            logger.warning("问答检索失败：%s", exc)
            yield SseEvent(
                "error",
                {"code": "retrieval_unavailable", "message": str(exc)[:300], "partial": False},
            )
            return

        threshold = settings.retrieval_score_threshold
        best_score = max((hit.score for hit in hits), default=0.0)
        if not hits or (threshold > 0 and best_score < threshold):
            reason = "no_context" if not hits else "low_score"
            for event in self._refusal_events(
                retrieval_ms=retrieval_ms,
                total_ms=(time.perf_counter() - started) * 1000,
                reason=reason,
            ):
                yield event
            return

        context, used_hits = build_context(hits, settings.llm_context_tokens)
        if not used_hits:
            # 预算连一段都放不下（理论上 truncate 会保住一段，这里是防御）
            for event in self._refusal_events(
                retrieval_ms=retrieval_ms,
                total_ms=(time.perf_counter() - started) * 1000,
                reason="no_context",
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
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated": True,
        }
        latency = {
            "retrieval": retrieval_ms,
            "first_token": int(first_token_ms) if first_token_ms is not None else 0,
            "total": int(total_ms),
        }

        done: dict[str, Any] = {
            "model": answer_model,
            "degraded": degraded,
            "grounded": grounded,
            "invalid_citations": len(invalid_ids),
            "usage": usage,
            "latency_ms": latency,
            "cache_hit": False,
        }
        if degraded:
            done["reason"] = "llm_failed"
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
            )

    # ---------- 缓存 ----------

    async def _load_cache(
        self, question: str, filters: SearchFilters | None
    ) -> dict[str, Any] | None:
        """
        读精确 QA 缓存。

        键包含：prompt 版本、模型名、语料版本（``documents:list:version``）、过滤条件摘要、
        问题摘要。语料版本来自 Redis 的列表版本号 —— 文档一写就变，旧答案自然不可达。

        :param question: 用户问题。
        :param filters: 过滤条件（参与键计算）。
        :return: 缓存内容；未命中 / 未启用 / Redis 异常时返回 ``None``。
        """
        if not self.redis or not settings.qa_cache_enabled:
            return None

        try:
            corpus_version = await get_list_version(self.redis)
        except Exception as exc:  # noqa: BLE001 - 版本取不到就按 0 算，仍可能命中
            logger.warning("读取语料版本失败，按 0 处理：%s", exc)
            corpus_version = 0

        try:
            return await cache_get(self.redis, self._cache_key(question, filters, corpus_version))
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
    ) -> None:
        """
        写精确 QA 缓存（best-effort，异常只告警）。

        :param question: 用户问题。
        :param filters: 过滤条件（参与键计算）。
        :param answer: 完整回答。
        :param citations: 本次提供的引用。
        :param grounded: 是否有有效引用。
        :param invalid_citations: 越界引用数量。
        :param usage: token 用量。
        :param model: 实际产出答案的模型（extractive 兜底时不是 LLM_MODEL）。
        """
        if not self.redis or not settings.qa_cache_enabled:
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
            }
            await cache_set(
                self.redis,
                self._cache_key(question, filters, corpus_version),
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
    ) -> str:
        """
        拼精确 QA 缓存键。

        过滤条件先序列化成稳定 JSON（``sort_keys``）再摘要，避免「字段顺序不同导致键不同」
        这种脏 miss；整体 key 里同时带版本与模型，任一变化都让旧键不可达。

        :param question: 用户问题。
        :param filters: 过滤条件。
        :param corpus_version: 语料版本号。
        :return: 缓存键。
        """
        filters_json = json.dumps(
            filters.model_dump() if filters else {},
            sort_keys=True,
            ensure_ascii=False,
        )
        filters_hash = hashlib.sha256(filters_json.encode("utf-8")).hexdigest()[:16]
        question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()
        return (
            f"{_QA_CACHE_PREFIX}:v{settings.prompt_version}:{settings.llm_model}:"
            f"{corpus_version}:{filters_hash}:{question_hash}"
        )

    def _replay_cache(self, cached: dict[str, Any]) -> list[SseEvent]:
        """
        把缓存内容回放成事件流（耗时全部记 0，``cache_hit=True``）。

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
                },
            )
        )
        return events

    def _refusal_events(
        self, *, retrieval_ms: int, total_ms: float, reason: str
    ) -> list[SseEvent]:
        """
        构造「不调 LLM 的拒答」事件流。

        :param retrieval_ms: 检索耗时（毫秒）。
        :param total_ms: 从请求开始到现在的耗时（毫秒）。
        :param reason: ``no_context``（没有召回）或 ``low_score``（分数低于阈值）。
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
                },
            ),
        ]
