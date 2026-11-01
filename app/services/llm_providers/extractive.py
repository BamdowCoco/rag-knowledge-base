"""
``extractive`` LLM provider： **不依赖模型** 的兜底档。

LLM 不可用（未装 Ollama、上游超时、首 token 前报错）时，与其返回 500 / 空回答，
不如把检索到的 top 片段按固定格式交给用户 —— 信息是检索给的， **不是模型编的** 。
这就是"降级但不失效"：``done.degraded=true`` 告诉调用方这只是兜底。

【为什么按 20–40 字符切块 yield】上层是按增量包 SSE ``token`` 事件的；一次性 yield 一整段，
前端看到的也是"唰"一下全出来。切成小块保留流式观感，且与真实 provider 的出参形状一致。
"""
from collections.abc import AsyncIterator
from typing import Any

from app.services.prompt_builder import REFUSAL_TEXT

# 兜底最多展示几条片段：再多就不像"引用资料"，像把检索结果倒出来
_MAX_HITS = 3

# 每段正文截到这个长度：兜底回答要短，用户想看全文会点引用跳原文
_CONTENT_LIMIT = 300

# 模拟流式的切块大小（20–40 字符之间）
_CHUNK_SIZE = 32


class ExtractiveProvider:
    """
    把检索片段拼成"资料列表"式回答的兜底 provider。
    """

    name = "extractive"

    async def stream(
        self,
        *,
        question: str,
        hits: list[Any],
        messages: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[str]:
        """
        用检索片段拼回答并分块产出。

        与 ``openai_compat`` 相反：这里只用 ``hits`` ，``messages`` 忽略。

        :param question: 用户问题（兜底文案里不直接回显，避免把用户输入当指令）。
        :param hits: 检索命中片段（取前 3 条）；为空时直接给拒答文案。
        :param messages: 已拼好的消息（忽略）。
        :param max_tokens: 生成上限（忽略：兜底回答长度由片段数决定）。
        :param temperature: 采样温度（忽略：无采样）。
        :return: 文本增量的异步迭代器。
        """
        if not hits:
            yield REFUSAL_TEXT
            return

        lines = ["模型暂不可用，以下是检索到的相关内容：", ""]
        for index, hit in enumerate(hits[:_MAX_HITS], start=1):
            # 兼容 dataclass / pydantic / dict 三种命中形状：统一用 getattr + 兜底
            title = getattr(hit, "title", None) or "（无标题）"
            source = getattr(hit, "source", None) or getattr(hit, "url", None) or ""
            content = (getattr(hit, "content", None) or "").strip()
            if len(content) > _CONTENT_LIMIT:
                content = content[:_CONTENT_LIMIT] + "…"

            lines.append(f"[{index}] 标题：{title}")
            if source:
                lines.append(f"来源：{source}")
            lines.append(f"内容：{content}")
            lines.append("")

        text = "\n".join(lines).rstrip()
        for start in range(0, len(text), _CHUNK_SIZE):
            yield text[start:start + _CHUNK_SIZE]
