"""
OpenAI 兼容协议的 **流式** chat 适配器：``POST {LLM_BASE_URL}{LLM_PATH}``。

Ollama / vLLM / 云厂商的兼容端点讲的都是这个协议（``stream=true`` + SSE），所以一个实现够起步。

【流长什么样】上游按行返回 ``data: {json}`` ，最后一行 ``data: [DONE]`` ；
正文增量藏在 ``choices[0].delta.content`` 里， **可能是 None** （首个 chunk 常只带 role，
最后一个 chunk 常只带 ``finish_reason``）—— 空内容要跳过，不能 yield 空串让上层误以为有输出。

【错误处理】
- 基址 / 路径没配 = 部署错误，立刻抛 ``LLMError``；
- HTTP 非 2xx 由 ``raise_for_status()`` 抛出（发生在 **首 token 之前** ，上层可降级）；
- 流里某行 ``data`` 不是合法 JSON = 上游协议不符，抛 ``LLMError`` 而不是静默跳过，
  否则用户会拿到一个"莫名其妙短了一截"的回答；空 ``data`` 行则跳过。
"""
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.core.config import settings
from app.infra.ai_client import get_llm_client

from .base import LLMError

logger = logging.getLogger(__name__)


class OpenAICompatLLMProvider:
    """
    走 OpenAI 兼容 ``/chat/completions`` 流式协议的 HTTP 上游。
    """

    name = "openai_compat"

    async def stream(
        self,
        *,
        question: str,
        hits: list[Any],
        messages: list[dict],
        max_tokens: int,
        temperature: float,
        usage_out: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """
        以 SSE 流的方式请求上游并逐段产出文本。

        ``hits`` 在这里用不到 —— prompt 已经由 ``prompt_builder`` 拼进 ``messages`` 了；
        参数保留是为了与协议一致。

        **usage：** 请求带 ``stream_options.include_usage``，上游最后一个 chunk 会带
        ``usage``（含 cache hit / miss token）；写进 ``usage_out`` 供费用与运维统计使用。
        不是所有兼容端点都支持该字段，不支持时会忽略，回答仍正常。

        :param question: 用户问题（仅用于日志）。
        :param hits: 检索片段（忽略）。
        :param messages: system + user 消息。
        :param max_tokens: 生成上限。
        :param temperature: 采样温度。
        :param usage_out: 可选的可变字典，用于接收上游 usage。
        :return: 文本增量的异步迭代器。
        :raises LLMError: 基址为空，或流中出现无法解析的 JSON。
        :raises httpx.HTTPStatusError: 上游返回非 2xx（流式请求在拿到响应头时即抛）。
        """
        if not settings.llm_base_url:
            raise LLMError("LLM_PROVIDER=openai_compat 但 LLM_BASE_URL 为空")

        # 两侧都归一，免得基址带尾斜杠 / 路径不带前斜杠
        url = f"{settings.llm_base_url.rstrip('/')}/{settings.llm_path.lstrip('/')}"
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = (
            {"Authorization": f"Bearer {settings.llm_api_key}"}
            if settings.llm_api_key
            else None
        )

        async with get_llm_client().stream(
            "POST", url, json=payload, headers=headers
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    # SSE 里还有 event: / :keep-alive 之类，只关心 data
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError as exc:
                    # 协议不符：宁可报错，也不要让用户拿到悄悄缺段的回答
                    raise LLMError(
                        f"LLM 流里的 data 不是合法 JSON：{data[:200]!r}"
                    ) from exc

                choices = chunk.get("choices") or []
                usage = chunk.get("usage")
                if usage and usage_out is not None:
                    # 最后一个 chunk 常只有 usage、没有 choices；必须在 continue 之前接住。
                    usage_out.clear()
                    usage_out.update(usage)
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if text:
                    yield text
