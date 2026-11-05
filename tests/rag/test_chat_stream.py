"""流式问答（``POST /api/v1/chat/stream``）：SSE 事件顺序、拒答、降级与引用校验。

测试环境 ``LLM_PROVIDER=extractive`` —— 不依赖 Ollama，也能把 citations / token / done
整条事件链跑通。要验证「LLM 失败降级」「非法引用」时，用 ``dependency_overrides``
换一个假 provider，并把 ``settings.llm_provider`` 从 ``extractive`` 改开
（否则 ``AnswerService`` 会强制使用兜底档，假 provider 根本不会被调用）。
"""
import json

from app.api.deps import get_llm_provider
from app.core.config import settings
from app.infra.qdrant_client import qdrant_client
from app.main import app
from app.services.embedding_providers.hash import HashProvider
from embedder.main import index_once

CHAT = "/api/v1/chat/stream"


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """
    把 SSE 响应文本解析成 ``(事件名, data)`` 列表。

    事件边界是 **空行**；一个事件里可能有多行 ``data:``（当前实现只发一行，这里仍按规范拼接）。

    :param text: 完整响应体。
    :return: 按出现顺序排列的事件列表。
    """
    events: list[tuple[str, dict]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        name: str | None = None
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        if name and data_lines:
            events.append((name, json.loads("\n".join(data_lines))))
    return events


async def _prepare(client, make_doc, external_id: str):
    """
    造一篇短文档并跑完向量化，让检索有东西可召回。

    :param client: ASGI 测试客户端。
    :param make_doc: 唯一内容文档工厂。
    :param external_id: 文档身份。
    :return: 文档 id。
    """
    doc = make_doc(
        source="feishu",
        external_id=external_id,
        title="部署手册",
        content="部署手册第一步：安装依赖。第二步：启动服务。",
    )
    document_id = (await client.post("/api/v1/documents", json=doc)).json()["id"]
    await index_once(client, qdrant_client, HashProvider(), limit=50)
    return document_id


def _done(events: list[tuple[str, dict]]) -> dict:
    """
    取出唯一的 ``done`` 事件（顺带断言它确实只出现一次）。

    :param events: 解析后的事件列表。
    :return: ``done`` 事件的 data。
    """
    done = [data for name, data in events if name == "done"]
    assert len(done) == 1, f"done 事件应该恰好出现一次，实际 {len(done)} 次"
    return done[0]


async def test_chat_stream_emits_citations_tokens_done(client, make_doc):
    """正常路径：先 citations、再若干 token、最后 done；回答带 ``[1]`` 引用。"""
    await _prepare(client, make_doc, "chat-1")

    r = await client.post(
        CHAT, json={"question": "部署手册第一步是什么？", "top_k": 5}
    )
    assert r.status_code == 200

    events = parse_sse(r.text)
    assert events
    assert events[0][0] == "citations"
    citations = events[0][1]["citations"]
    assert citations
    assert citations[0]["n"] == 1
    assert citations[0]["title"] == "部署手册"

    tokens = [data["text"] for name, data in events if name == "token"]
    assert tokens
    answer = "".join(tokens)
    assert answer.strip()
    assert "[1]" in answer                     # extractive 兜底会带编号引用
    assert "检索片段模式" in answer             # 主动 extractive 是演示模式，不是故障
    assert "模型暂不可用" not in answer

    assert events[-1][0] == "done"
    done = _done(events)
    assert done["degraded"] is False
    assert done["grounded"] is True
    assert done["cache_hit"] is False
    assert "reason" not in done


async def test_chat_refuses_when_no_context(client, make_doc):
    """过滤到不存在的文档 → 不调 LLM，直接固定拒答，``reason=no_context``。"""
    await _prepare(client, make_doc, "chat-2")

    r = await client.post(
        CHAT,
        json={
            "question": "部署手册第一步是什么？",
            "top_k": 5,
            "filters": {"document_ids": [999999]},
        },
    )
    assert r.status_code == 200

    events = parse_sse(r.text)
    assert events[0][0] == "citations"
    assert events[0][1]["citations"] == []

    answer = "".join(data["text"] for name, data in events if name == "token")
    assert "无法确定" in answer

    done = _done(events)
    assert done["reason"] == "no_context"


class _FailingLLM:
    """首 token 之前就抛错：触发 ``AnswerService`` 的 extractive 降级。"""

    name = "failing"

    async def stream(self, **kwargs):
        """
        迭代时立刻失败（函数含 ``yield``，因此调用返回的是异步生成器）。

        :raises RuntimeError: 固定失败。
        """
        raise RuntimeError("boom")
        yield ""  # pragma: no cover - 让函数成为异步生成器；raise 一定先执行


async def test_llm_failure_degrades_to_extractive(client, make_doc, monkeypatch):
    """LLM 首 token 前失败 → 用检索片段兜底，``done.degraded=True``，不是 500。"""
    await _prepare(client, make_doc, "chat-3")
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    app.dependency_overrides[get_llm_provider] = lambda: _FailingLLM()
    try:
        r = await client.post(
            CHAT, json={"question": "部署手册第一步是什么？", "top_k": 5}
        )
    finally:
        app.dependency_overrides.pop(get_llm_provider, None)

    assert r.status_code == 200
    events = parse_sse(r.text)
    answer = "".join(data["text"] for name, data in events if name == "token")
    assert answer.strip()
    assert "[1]" in answer                     # 降级答案来自 extractive
    assert "模型暂不可用" in answer             # LLM 失败兜底才用这句

    done = _done(events)
    assert done["degraded"] is True
    assert done["reason"] == "llm_failed"


class _BadCitationLLM:
    """返回一个越界引用 ``[9]``：验证引用校验只统计、不改已流出的文本。"""

    name = "bad-citation"

    async def stream(self, **kwargs):
        """
        产出一段带越界引用的回答。

        :yield: 固定回答文本。
        """
        yield "答案 [9]"


async def test_invalid_citation_is_flagged(client, make_doc, monkeypatch):
    """回答引用了不存在的资料编号 → ``invalid_citations=1``、``grounded=False``。"""
    await _prepare(client, make_doc, "chat-4")
    monkeypatch.setattr(settings, "llm_provider", "openai_compat")
    app.dependency_overrides[get_llm_provider] = lambda: _BadCitationLLM()
    try:
        r = await client.post(
            CHAT, json={"question": "部署手册第一步是什么？", "top_k": 5}
        )
    finally:
        app.dependency_overrides.pop(get_llm_provider, None)

    assert r.status_code == 200
    events = parse_sse(r.text)
    answer = "".join(data["text"] for name, data in events if name == "token")
    assert "答案" in answer

    done = _done(events)
    assert done["invalid_citations"] == 1
    assert done["grounded"] is False
