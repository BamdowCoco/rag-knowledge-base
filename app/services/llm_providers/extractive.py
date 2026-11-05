"""
``extractive`` LLM provider： **不依赖模型** 的兜底/演示档。

两种用途，提示语不同：

- **主动选择** （``LLM_PROVIDER=extractive``）：告诉用户“当前为检索片段模式（未接大模型）”，
  这不是故障，只是没有生成式模型；
- **LLM 首 token 前失败**：告诉用户“模型暂不可用”，当前显示的是检索到的相关内容。

【片段展示的取舍】检索 top-k 里经常出现同一页面的多个相邻 chunk，直接按原顺序拼会出现
重复标题、半句开头和 ``¶`` 锚点。这里先按 ``document_id`` 去重，同一文档的相邻 chunk
按 ``chunk_index`` 合并（能识别重叠就直接接上），再按句子边界截断；每个文档最多展示
``_CONTENT_LIMIT`` 字。纯文本展示不消耗 LLM token，长度可以比原来宽一些。

【为什么按 20–40 字符切块 yield】上层是按增量包 SSE ``token`` 事件的；一次性 yield 一整段，
前端看到的也是“唰”一下全出来。切成小块保留流式观感，且与真实 provider 的出参形状一致。
"""
import re
from collections.abc import AsyncIterator
from typing import Any

from app.services.prompt_builder import REFUSAL_TEXT

# 展示给用户的默认提示（LLM 首 token 前失败的兜底）
DEFAULT_FALLBACK_NOTICE = "模型暂不可用，以下是检索到的相关内容："

# 主动选择 extractive 时的提示：不是故障，是演示模式
DEMO_NOTICE = "当前为检索片段模式（未接大模型），以下是检索到的相关内容："

# 最多展示几个不同文档：再多就不像“引用资料”，像把检索结果倒出来
_MAX_DOCS = 3

# 每个文档合并后保留的字数上限：纯文本不耗 token，但要保证回答不至于太长
_CONTENT_LIMIT = 600

# 合并相邻 chunk 时，允许识别的最短重叠长度（避免把正常重复的短句误删）
_MIN_OVERLAP = 20
_MAX_OVERLAP = 200

# 句子边界：优先在这些字符之后截断
_SENTENCE_ENDINGS = "。！？；.!?;\n"

# 模拟流式的切块大小（20–40 字符之间）
_CHUNK_SIZE = 32

# 连续空行压缩（保留段落分隔，不把代码块压没）
_MULTI_NEWLINE = re.compile(r"\n{3,}")


def _field(hit: Any, name: str, default: Any = None) -> Any:
    """
    统一读取命中片段的字段，兼容 dataclass / pydantic / dict。

    :param hit: 命中片段。
    :param name: 字段名。
    :param default: 字段缺失时的默认值。
    :return: 字段值。
    """
    if isinstance(hit, dict):
        return hit.get(name, default)
    return getattr(hit, name, default)


def _clean_snippet(text: str) -> str:
    """
    去掉标题锚点 ``¶``、行尾空白与过多空行，保留段落与代码缩进。

    :param text: 原始 chunk 正文。
    :return: 清理后的正文。
    """
    cleaned = (text or "").replace("¶", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in cleaned.splitlines()]
    return _MULTI_NEWLINE.sub("\n\n", "\n".join(lines)).strip()


def _merge_text(left: str, right: str) -> str:
    """
    拼接同一文档的相邻 chunk，尽量去掉切块重叠。

    切片按 token/段落切，相邻块常有一段重复（overlap）。这里从长到短找
    “left 的后缀 == right 的前缀”，找到就只补 right 剩余部分；找不到就换行拼接。

    :param left: 已合并的文本。
    :param right: 下一块文本。
    :return: 合并后的文本。
    """
    if not left:
        return right
    if not right:
        return left

    max_overlap = min(len(left), len(right), _MAX_OVERLAP)
    for size in range(max_overlap, _MIN_OVERLAP - 1, -1):
        if left.endswith(right[:size]):
            return left + right[size:]
    return f"{left}\n{right}"


def _truncate_at_sentence(text: str, limit: int) -> str:
    """
    在句子边界截断正文；找不到合适边界时退回硬截断。

    :param text: 已合并的正文。
    :param limit: 最大字符数。
    :return: 截断后的正文（超限时以 ``…`` 结尾）。
    """
    if len(text) <= limit:
        return text

    window = text[:limit]
    cut = max(window.rfind(char) for char in _SENTENCE_ENDINGS)
    if cut >= limit // 2:
        return text[: cut + 1].rstrip() + "…"
    return window.rstrip() + "…"


def _strip_leading_title(text: str, title: str) -> str:
    """
    去掉正文开头与标题重复的标题行。

    chunk 正文常从页面 ``<h1>`` 开始，展示时又已经有一行 ``[n] 标题``；
    不去掉会看到“依赖项 / 依赖项”重复。

    :param text: 合并后的正文。
    :param title: 展示用标题。
    :return: 去掉开头重复标题行后的正文。
    """
    target = title.strip()
    lines = text.splitlines()
    while lines and lines[0].strip() == target:
        lines.pop(0)
    return "\n".join(lines).lstrip()


async def _iter_documents(hits: list[Any]) -> AsyncIterator[tuple[str, str]]:
    """
    按文档去重并合并相邻 chunk，产出 ``(标题, 正文)`` 的异步迭代器。

    命中顺序保留“第一次出现”的顺序；同一 ``document_id`` 的 chunk 按 ``chunk_index``
    排序后合并。没有 ``document_id`` 时退回 URL 或对象 id。

    :param hits: 检索命中片段。
    :return: 每个不同文档一条 ``(标题, 合并正文)``。
    """
    groups: dict[Any, list[Any]] = {}
    order: list[Any] = []
    for hit in hits:
        key = _field(hit, "document_id")
        if key is None:
            key = _field(hit, "url") or id(hit)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(hit)

    for key in order[:_MAX_DOCS]:
        chunks = sorted(groups[key], key=lambda item: _field(item, "chunk_index", 0))
        title = ""
        merged = ""
        for chunk in chunks:
            title = title or (_field(chunk, "title") or "")
            content = _clean_snippet(_field(chunk, "content") or "")
            merged = _merge_text(merged, content)
        if merged:
            title = title or "（无标题）"
            merged = _strip_leading_title(merged, title)
            if merged:
                yield title, merged


class ExtractiveProvider:
    """
    把检索片段整理成“资料列表”式回答的兜底/演示 provider。

    :param notice: 回答首行的提示语；主动演示与 LLM 失败兜底使用不同文案。
    """

    name = "extractive"

    def __init__(self, notice: str = DEFAULT_FALLBACK_NOTICE) -> None:
        """
        :param notice: 回答开头的提示语。
        """
        self.notice = notice

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
        用检索片段拼回答并分块产出。

        与 ``openai_compat`` 相反：这里只用 ``hits`` ，``messages`` 忽略。

        :param question: 用户问题（兜底文案里不直接回显，避免把用户输入当指令）。
        :param hits: 检索命中片段（按文档去重，最多 3 个文档）。
        :param messages: 已拼好的消息（忽略）。
        :param max_tokens: 生成上限（忽略：兜底回答长度由片段数决定）。
        :param temperature: 采样温度（忽略：无采样）。
        :param usage_out: 上游 usage 容器（extractive 没有真实模型，忽略）。
        :return: 文本增量的异步迭代器。
        """
        _ = usage_out
        if not hits:
            yield REFUSAL_TEXT
            return

        lines = [self.notice, ""]
        index = 0
        async for title, content in _iter_documents(hits):
            index += 1
            lines.append(f"[{index}] {title}")
            lines.append(_truncate_at_sentence(content, _CONTENT_LIMIT))
            lines.append("")

        if index == 0:
            yield REFUSAL_TEXT
            return

        text = "\n".join(lines).rstrip()
        for start in range(0, len(text), _CHUNK_SIZE):
            yield text[start : start + _CHUNK_SIZE]
