"""
LLM 适配层的公共底座：协议 + 错误类型。

两个 provider（``openai_compat`` / ``extractive``）出参都是 **文本增量的异步迭代器** ——
上层 ``answer_service`` 只管把增量包成 SSE ``token`` 事件，不必知道文本从哪来。

【两种输入，各取所需】``openai_compat`` 用 ``messages`` （已拼好的 system + user prompt）；
``extractive`` 不用模型，直接用 ``hits`` 拼检索片段。协议同时给出两者，
provider 只读自己要的那一份，另一份忽略。
"""
from collections.abc import AsyncIterator
from typing import Any, Protocol


class LLMError(Exception):
    """
    LLM 调用失败：配置错误（基址没配）、上游不可用、流里出现无法解析的 JSON 等。

    ``answer_service`` 靠"首 token 之前有没有抛"来决定是否降级到 ``extractive``。
    """


class LLMProvider(Protocol):
    """
    一个 LLM provider 的形状。

    - ``stream`` 返回 **文本增量** 的异步迭代器；
    - **失败要抛** （``LLMError`` 或底层异常）：不能把错误吞成一个空回答，
      上层没法区分"模型拒答"和"模型挂了"。
    """

    name: str

    def stream(
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
        流式生成回答。

        :param question: 用户问题（``extractive`` 用来做说明书式前缀；也便于日志）。
        :param hits: 检索命中的片段（``extractive`` 用；可带 dataclass 属性）。
        :param messages: 已拼好的对话消息（``openai_compat`` 用）。
        :param max_tokens: 生成上限。
        :param temperature: 采样温度。
        :param usage_out: 可选的可变字典；provider 把上游返回的 usage 写进去
            （token / cache hit-miss）。不支持的 provider 忽略。
        :return: 文本增量的异步迭代器（每次 ``yield`` 一段非空文本）。
        :raises LLMError: 配置错误或上游流解析失败。
        """
        ...
