"""``extractive`` provider：文案可配置、按文档去重、合并 chunk、句子边界截断。"""
from types import SimpleNamespace

from app.services.llm_providers.extractive import (
    DEMO_NOTICE,
    ExtractiveProvider,
)
from app.services.prompt_builder import REFUSAL_TEXT


def _hit(
    *,
    document_id: int,
    chunk_index: int,
    content: str,
    title: str = "标题",
) -> SimpleNamespace:
    """
    造一个最小检索命中。

    :param document_id: 文档版本 id。
    :param chunk_index: 块序号。
    :param content: 正文。
    :param title: 标题。
    :return: 带属性的简单对象。
    """
    return SimpleNamespace(
        document_id=document_id,
        chunk_index=chunk_index,
        content=content,
        title=title,
        source="web",
        url=f"https://example.com/doc/{document_id}",
    )


async def _collect(provider: ExtractiveProvider, hits: list[SimpleNamespace]) -> str:
    """
    跑完 provider 的流，拼成完整文本。

    :param provider: extractive provider。
    :param hits: 命中片段。
    :return: 完整回答文本。
    """
    pieces = [
        piece
        async for piece in provider.stream(
            question="测试问题",
            hits=hits,
            messages=[],
            max_tokens=512,
            temperature=0.0,
        )
    ]
    return "".join(pieces)


async def test_notice_is_configurable() -> None:
    """主动演示与 LLM 失败兜底可以使用不同首行。"""
    provider = ExtractiveProvider(notice="自定义提示：")
    text = await _collect(provider, [_hit(document_id=1, chunk_index=0, content="正文内容")])

    assert text.startswith("自定义提示：")
    assert "正文内容" in text


async def test_demo_notice_is_not_a_failure_message() -> None:
    """主动 extractive 的默认演示文案不应出现“模型暂不可用”。"""
    provider = ExtractiveProvider(notice=DEMO_NOTICE)
    text = await _collect(provider, [_hit(document_id=1, chunk_index=0, content="正文")])

    assert "检索片段模式" in text
    assert "模型暂不可用" not in text


async def test_same_document_is_deduplicated_and_chunks_merged() -> None:
    """同一文档的多个 chunk 只输出一段，并保留各段内容。"""
    hits = [
        _hit(document_id=1, chunk_index=0, content="第一段内容。"),
        _hit(document_id=1, chunk_index=1, content="第二段内容。"),
        _hit(document_id=1, chunk_index=2, content="第三段内容。"),
    ]

    text = await _collect(ExtractiveProvider(), hits)

    assert text.count("[1]") == 1
    assert "[2]" not in text
    assert "第一段内容" in text
    assert "第二段内容" in text
    assert "第三段内容" in text


async def test_multiple_documents_are_kept_until_three() -> None:
    """不同文档最多展示 3 个，第 4 个不输出。"""
    hits = [
        _hit(document_id=index, chunk_index=0, content=f"文档{index}的内容。")
        for index in range(1, 5)
    ]

    text = await _collect(ExtractiveProvider(), hits)

    assert "[1]" in text
    assert "[2]" in text
    assert "[3]" in text
    assert "[4]" not in text
    assert "文档4" not in text


async def test_paragraph_marker_is_removed() -> None:
    """mkdocs 的 ``¶`` 锚点不应出现在回答正文里。"""
    hit = _hit(document_id=1, chunk_index=0, content="依赖项¶\n正文内容。")

    text = await _collect(ExtractiveProvider(), [hit])

    assert "¶" not in text
    assert "正文内容" in text


async def test_leading_heading_equal_to_title_is_removed() -> None:
    """正文开头的标题行与 ``[n] 标题`` 重复时只保留一次。"""
    hit = _hit(document_id=1, chunk_index=0, title="依赖项", content="依赖项\n正文内容。")

    text = await _collect(ExtractiveProvider(), [hit])

    assert text.count("依赖项") == 1
    assert "正文内容" in text


async def test_long_content_is_truncated_at_sentence_boundary() -> None:
    """长正文在句子边界截断，并以省略号结尾。"""
    content = "甲" * 300 + "。" + "乙" * 400
    hit = _hit(document_id=1, chunk_index=0, content=content)

    text = await _collect(ExtractiveProvider(), [hit])

    assert text.endswith("…")
    assert "乙乙乙" not in text


async def test_empty_hits_returns_refusal() -> None:
    """没有命中时仍返回固定拒答。"""
    text = await _collect(ExtractiveProvider(), [])

    assert REFUSAL_TEXT in text
