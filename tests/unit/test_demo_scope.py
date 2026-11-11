"""访客模式的范围收窄：来源与召回数由服务端兜底。

Caddy 在 ``/demo/*`` 反代时注入 ``X-Demo-Mode: 1``；前端虽然已经锁死筛选项，
但直接调接口仍可能传任意 ``filters`` / ``top_k``，所以这里验证兜底逻辑本身。

**语言不单独过滤**：``source=web`` 就是 FastAPI 中文文档；``language`` 字段由
enrichment 补写，晚于向量写入，历史 chunk 的 payload 可能为空，硬过滤会误伤。
"""
from app.api.v1.endpoints.qa import _demo_scope
from app.schemas.qa import SearchFilters


def test_demo_scope_overrides_source_and_topk():
    """访客请求即使传 feishu / en / top_k=10，也会被收窄为 web / 5。"""
    top_k, filters = _demo_scope(
        True, 10, SearchFilters(source=["feishu"], language="en")
    )

    assert top_k == 5
    assert filters is not None
    assert filters.source == ["web"]
    assert filters.language is None


def test_demo_scope_builds_filters_when_missing():
    """访客没有传 filters 时，也要生成 web 过滤条件。"""
    top_k, filters = _demo_scope(True, 3, None)

    assert top_k == 5
    assert filters is not None
    assert filters.source == ["web"]
    assert filters.language is None


def test_non_demo_scope_is_untouched():
    """站主路径不带访客标记，请求体原样透传。"""
    filters = SearchFilters(source=["feishu"], language="en")

    top_k, returned = _demo_scope(False, 10, filters)

    assert top_k == 10
    assert returned is filters
