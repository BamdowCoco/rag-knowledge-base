"""
enrich 适配层（阶段 H2）：注册表 + 形状翻译 + 降级链。

**这里最要紧的两条：**
① 发给上游的是 **样本** ，不是全文 —— 那是 Q3 里标出的成本洞；
② **解析失败必须抛** ，不能默默留 NULL —— 否则"接错了"会被伪装成"这篇文档没有摘要"。

这两条都是"不测就会悄悄退化"的地方，所以单独钉住。
"""
import json

import httpx
import pytest

from app.core.config import settings
from app.infra.model_client import get_model_client
from app.services.enrich_providers import ENRICH_VERSION, RULE, available, get_provider
from app.services.enrich_providers.openai_compat import OpenAICompatProvider
from app.services.enrich_service import enrich
from app.utils.sample import sample_text

# 五段：首段点题、尾段给结论，中间是大段细节 —— 中间那几段不该出现在样本里
LONG_DOC = "\n\n".join(
    [
        "首段：接口规范全文。",
        "中间细节之一：所有接口必须返回统一的错误结构，分页统一用 page 与 limit。",
        "中间细节之二：时间一律 UTC，跨时区展示交给前端处理。",
        "中间细节之三：大字段不要放在列表接口里，避免响应体过大。",
        "尾段：结论 —— 入库成功才是硬要求，AI 只是增强。",
    ]
)


def _openai_body(content: str, *, finish_reason: str = "stop") -> dict:
    """造一个 OpenAI 兼容形状的响应体。"""
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
    }


# 结果藏在 content 里的是一段 JSON 字符串（不是对象）—— 这正是适配层要解析的东西
OPENAI_BODY = _openai_body(
    json.dumps(
        {"summary": "模型给的摘要", "keywords": ["模型词"], "language": "zh"}, ensure_ascii=False
    )
)


class Recorder:
    """把 ``post`` 换掉，顺便把请求记下来 —— 断言"发出去的是什么"全靠它。"""

    def __init__(self, body: dict, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.calls: list[dict] = []

    async def __call__(
        self, url: str, json: object = None, headers: dict | None = None
    ) -> httpx.Response:
        self.calls.append({"url": url, "payload": json, "headers": headers})
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))


def install(monkeypatch, recorder) -> Recorder:
    monkeypatch.setattr(get_model_client(), "post", recorder)
    return recorder


# ---------- 注册表 ----------

def test_registry_lists_the_providers():
    assert available() == ["model", "openai_compat", "rule"]
    assert get_provider("rule") is RULE                 # 降级终点是单例
    assert get_provider("openai_compat").name == "openai_compat"


# ---------- openai_compat：请求形状 ----------

async def test_openai_compat_sends_messages_and_max_tokens(monkeypatch):
    """请求体是 messages 数组（不是 ``{text}``），长度靠 ``max_tokens`` 控 —— 这就是"另一种形状"。"""
    rec = install(monkeypatch, Recorder(OPENAI_BODY))

    await OpenAICompatProvider().enrich(LONG_DOC)

    call = rec.calls[0]
    assert call["url"].endswith("/v1/chat/completions")
    payload = call["payload"]
    assert payload["model"] == settings.enrich_model_name
    assert payload["max_tokens"] == settings.enrich_max_tokens      # ← 顺带补上的成本闸门
    assert payload["temperature"] == 0
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]


async def test_sent_content_is_a_sample_not_the_whole_document(monkeypatch):
    """
     **发出去的是样本** ：首段 + 尾段，中间整段不发 —— 长文档按输入全长计费，整篇发是白烧钱。
    """
    rec = install(monkeypatch, Recorder(OPENAI_BODY))

    await OpenAICompatProvider().enrich(LONG_DOC)

    sent = rec.calls[0]["payload"]["messages"][1]["content"]
    assert "首段" in sent and "尾段" in sent        # 两头都在
    assert "中间细节之二" not in sent                # 中间没发出去
    assert len(sent) < len(LONG_DOC)


async def test_api_key_is_optional(monkeypatch):
    """自建推理（Ollama / vLLM）不需要 key —— 所以没配就不带 ``Authorization``。"""
    rec = install(monkeypatch, Recorder(OPENAI_BODY))

    monkeypatch.setattr(settings, "enrich_api_key", "")
    await OpenAICompatProvider().enrich("正文")
    assert rec.calls[0]["headers"] is None

    monkeypatch.setattr(settings, "enrich_api_key", "sk-test")
    await OpenAICompatProvider().enrich("正文")
    assert rec.calls[1]["headers"] == {"Authorization": "Bearer sk-test"}


async def test_path_and_json_mode_are_configurable(monkeypatch):
    """
    两处"厂商差异"的旋钮： **端点路径** （GLM 那类不是 ``/v1/chat/completions``）与
    **JSON 模式** （默认关 —— 不是所有兼容实现都支持 ``response_format``，不支持会 400）。
    """
    rec = install(monkeypatch, Recorder(OPENAI_BODY))

    # ① 默认：标准路径，且不带 response_format
    # （两个前提都显式钉住 —— 别读环境默认值：`.env` 里接真厂商时会把它改成 true）
    monkeypatch.setattr(settings, "enrich_openai_base_url", "http://model:8000")
    monkeypatch.setattr(settings, "enrich_openai_path", "/v1/chat/completions")
    monkeypatch.setattr(settings, "enrich_json_mode", False)
    await OpenAICompatProvider().enrich("正文")
    assert rec.calls[0]["url"] == "http://model:8000/v1/chat/completions"
    assert "response_format" not in rec.calls[0]["payload"]

    # ② 换成 GLM 那类路径（故意不写开头的 `/`，验证会归一）+ 打开 JSON 模式
    monkeypatch.setattr(settings, "enrich_openai_base_url", "https://open.bigmodel.cn/api/paas/")
    monkeypatch.setattr(settings, "enrich_openai_path", "v4/chat/completions")
    monkeypatch.setattr(settings, "enrich_json_mode", True)
    await OpenAICompatProvider().enrich("正文")
    assert rec.calls[1]["url"] == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert rec.calls[1]["payload"]["response_format"] == {"type": "json_object"}


# ---------- openai_compat：响应解析 ----------

async def test_openai_compat_parses_choices_and_fills_author(monkeypatch):
    install(monkeypatch, Recorder(OPENAI_BODY))

    out = await OpenAICompatProvider().enrich("作者：张三\n\n" + LONG_DOC)

    assert out["ai_summary"] == "模型给的摘要"        # 来自 choices[0].message.content
    assert out["tags"] == ["模型词"]
    assert out["language"] == "zh"
    assert out["author"] == "张三"                   # 署名仍走规则（模型没有额外信息）
    assert out["enrich_version"] == ENRICH_VERSION


async def test_openai_compat_strips_code_fence(monkeypatch):
    """有些实现会把 JSON 包在 ```json … ``` 里 —— 剥掉再解析。"""
    fenced = "```json\n" + json.dumps({"summary": "带围栏的摘要", "language": "zh"}) + "\n```"
    install(monkeypatch, Recorder(_openai_body(fenced)))

    out = await OpenAICompatProvider().enrich("正文")

    assert out["ai_summary"] == "带围栏的摘要"


@pytest.mark.parametrize(
    "body",
    [
        {},                                    # 连 choices 都没有
        {"choices": [{"message": {}}]},        # 有 choices 但 content 是空的
        _openai_body("这不是 JSON"),            # content 不是 JSON
    ],
)
async def test_openai_compat_raises_when_the_response_is_not_shaped_right(monkeypatch, body):
    """
    **必须抛** （而不是留 NULL）—— 形状不对是"接法错了"，不是"这篇文档没有摘要"。
    静默留空会把这两件事混成一个，排查时无从下手。
    """
    install(monkeypatch, Recorder(body))

    with pytest.raises(Exception):
        await OpenAICompatProvider().enrich("正文")


# ---------- 降级链（facade 层）----------

async def test_facade_dispatches_to_openai_compat(monkeypatch):
    install(monkeypatch, Recorder(OPENAI_BODY))
    monkeypatch.setattr(settings, "enrich_provider", "openai_compat")

    out = await enrich({"content": LONG_DOC}, {})

    assert out["ai_summary"] == "模型给的摘要"


async def test_openai_compat_failure_falls_back_to_rule(monkeypatch):
    """
     **降级链** ：上游不可达 / 形状不对 → 回落规则 → 几列 **仍有值** （不是 NULL）。
    """
    async def boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("上游不可达")

    install(monkeypatch, boom)
    monkeypatch.setattr(settings, "enrich_provider", "openai_compat")

    out = await enrich({"content": LONG_DOC}, {})

    assert out["language"] == "zh"
    # " … " 是规则侧摘要的分隔符 —— 值确实来自规则，而不是上游
    assert out["ai_summary"] and " … " in out["ai_summary"]
    assert out["enrich_version"] == ENRICH_VERSION


# ---------- sample_text（纯函数）----------

def test_sample_keeps_both_ends_and_marks_the_gap():
    """
    两头都留、中间丢掉，而且要 **明确写出省略标记** ——
    用"…"这种含糊符号会混进正文，让"这份摘要是谁给的"看不出来（验收时吃过这个亏）。
    """
    out = sample_text(LONG_DOC, limit=4000)
    assert "首段" in out and "尾段" in out
    assert "中间细节" not in out
    assert "[中间省略]" in out


def test_sample_of_short_text_is_untouched():
    assert sample_text("只有一段话。", limit=4000) == "只有一段话。"
    text = "第一段。\n\n第二段。"
    assert sample_text(text, limit=4000) == text


def test_sample_never_exceeds_the_limit():
    """上限是给钱包设的，不能失守 —— 单段超长时也如此。"""
    assert len(sample_text("啊" * 5000, limit=100)) <= 100
    assert len(sample_text(LONG_DOC, limit=30)) <= 30


def test_sample_of_blank_is_empty():
    assert sample_text("", limit=100) == ""
    assert sample_text("  \n\n  ", limit=100) == ""
