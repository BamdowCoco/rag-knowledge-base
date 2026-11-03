"""
``GET /metrics``：rag / all 角色暴露 Prometheus 文本指标，且 HTTP 计数随请求增长。

测试环境 ``APP_ROLE=all``（见 compose 的 test 服务），所以顶层 ``/metrics`` 一定存在。
``ASGITransport`` 不跑 lifespan —— ``metrics_loop`` 不会启动，``rag_pipeline_pending_chunks``
保持在注册时的 0 值；这不影响「指标已注册并曝光」的断言。
"""

_METRIC_NAMES = (
    "rag_http_requests_total",
    "rag_retrieval_latency_seconds",
    "rag_pipeline_pending_chunks",
)


def _sample_value(body: str, prefix: str) -> float:
    """
    从 Prometheus 文本里取出某条样本的数值。

    只按文本前缀匹配、不依赖指标出现的顺序，也不假设测试之间的计数从 0 开始 ——
    同一会话里其它测试也可能打过同一路由。

    :param body: ``/metrics`` 的响应正文。
    :param prefix: 样本前缀，例如 ``rag_http_requests_total{route="/health",status="200"}``。
    :return: 该样本的当前值；样本不存在时返回 0。
    """
    for line in body.splitlines():
        if line.startswith(prefix + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


async def test_metrics_endpoint_exposes_rag_metrics(client):
    """``/metrics`` 返回 200 与 text/plain，正文含关键指标名。"""
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]

    body = response.text
    for name in _METRIC_NAMES:
        assert name in body, f"缺少指标 {name}"


async def test_http_request_counter_increases(client):
    """发一次 ``/health`` 后，对应 route/status 的请求计数增加。"""
    # 中间件按路由模板打 label；/health 是固定路径，模板与实际 URL 一致
    prefix = 'rag_http_requests_total{route="/health",status="200"}'
    before = _sample_value((await client.get("/metrics")).text, prefix)

    assert (await client.get("/health")).status_code == 200

    after = _sample_value((await client.get("/metrics")).text, prefix)
    assert after >= before + 1, "请求计数没有随 /health 调用增长"
