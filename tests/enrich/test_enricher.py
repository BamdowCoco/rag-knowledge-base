"""
enrich 的后台补（阶段 H）：provider 三态 + 降级链 + 状态机。

**这里最要紧的一条是「请求路径里不该有模型调用」** —— 那是本阶段结构改动的全部理由：
``bulk_ingest`` 是逐篇走的，模型调用若留在请求路径里，同步器推 200 篇就要 10 分钟。
"""
import httpx

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.infra.model_client import get_model_client
from app.models.document_clean import DocumentClean, EnrichStatus
from app.services.enricher import enrich_once

DOC = {
    "title": "接口规范",
    "content": (
        "作者：张三\n\n"
        "所有接口必须返回统一的错误结构，分页统一用 page 与 limit。\n\n"
        "最后一段：时间一律 UTC。"
    ),
}


async def _row(document_id: int) -> DocumentClean:
    async with AsyncSessionLocal() as db:
        row = await db.get(DocumentClean, document_id)
        assert row is not None
        return row


# ---------- 核心断言：请求路径里没有模型调用 ----------

async def test_ingest_never_calls_the_model(client, make_doc, monkeypatch):
    """
     **哪怕 provider=model，POST 也不该调模型** —— 它只把文档标成 pending。

    这就是"慢的外部依赖不进请求路径"的落点。
    """
    calls: list[str] = []

    async def spy(*args: object, **kwargs: object) -> httpx.Response:
        calls.append("called")
        raise AssertionError("请求路径里不该调模型！")

    monkeypatch.setattr(settings, "enrich_provider", "model")
    monkeypatch.setattr(get_model_client(), "post", spy)

    created = (await client.post("/api/v1/documents", json=make_doc(**DOC))).json()

    assert calls == []                                   # 一次都没调
    assert created["ai_summary"] is None                 # 响应里还是空的
    assert (await _row(created["id"])).enrich_status == EnrichStatus.PENDING


# ---------- provider 三态 ----------

async def test_rule_provider(client, make_doc, monkeypatch):
    """默认：走纯 Python 那套。"""
    monkeypatch.setattr(settings, "enrich_provider", "rule")
    created = (await client.post("/api/v1/documents", json=make_doc(**DOC))).json()

    assert await enrich_once() == 1

    row = await _row(created["id"])
    assert row.enrich_status == EnrichStatus.DONE
    assert row.language == "zh"
    assert row.author == "张三"
    assert row.tags                                        # 关键词
    assert row.enrich_version == 1


async def test_off_provider_leaves_everything_null_and_pending(client, make_doc, monkeypatch):
    """``off``：**不去认领**，几列保持 NULL、状态留在 pending —— 开关一打开还能补上。"""
    monkeypatch.setattr(settings, "enrich_provider", "off")
    created = (await client.post("/api/v1/documents", json=make_doc(**DOC))).json()

    assert await enrich_once() == 0

    row = await _row(created["id"])
    assert row.enrich_status == EnrichStatus.PENDING        # 仍是待办
    assert row.language is None and row.ai_summary is None and row.tags is None
    assert row.enrich_version is None                       # "没做过"


async def test_model_provider_uses_the_service(client, make_doc, monkeypatch):
    """``model``：值来自 **模型服务** （而不是规则）—— 一眼能看出是谁给的。"""
    async def fake_post(url: str, json: object = None) -> httpx.Response:
        return httpx.Response(
            200,
            json={"summary": "模型给的摘要", "keywords": ["模型词A", "模型词B"], "language": "zh"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(settings, "enrich_provider", "model")
    monkeypatch.setattr(get_model_client(), "post", fake_post)
    created = (await client.post("/api/v1/documents", json=make_doc(**DOC))).json()

    assert await enrich_once() == 1

    row = await _row(created["id"])
    assert row.ai_summary == "模型给的摘要"
    assert row.tags == ["模型词A", "模型词B"]
    # 署名仍走规则（模型对署名没有额外信息）
    assert row.author == "张三"


# ---------- 降级链 ----------

async def test_model_failure_falls_back_to_rule(client, make_doc, monkeypatch):
    """
     **降级链**：模型不可达 → 回落规则 → 几列**仍有值** （不是 NULL），入库照样成功。
    """
    async def boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("模型服务不可达")

    monkeypatch.setattr(settings, "enrich_provider", "model")
    monkeypatch.setattr(get_model_client(), "post", boom)
    created = (await client.post("/api/v1/documents", json=make_doc(**DOC))).json()

    assert await enrich_once() == 1                     # 没有抛，照样补完

    row = await _row(created["id"])
    assert row.enrich_status == EnrichStatus.DONE
    assert row.language == "zh" and row.author == "张三"   # 规则给的
    assert row.tags
