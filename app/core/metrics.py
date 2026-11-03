"""
Prometheus 指标定义与轻量 helper。

**指标只做可观测，不参与业务判断。** 这里的函数不做任何分支决策，只往注册表里
``inc`` / ``observe`` / ``set``；调用方即使量到异常值（例如耗时 0、结果 0 条），
请求语义也不变。反过来，业务代码也不直接拼 label —— 统一走下面的 helper，
避免同一个指标在不同调用点写出拼写不一致的 label 值（那会在面板上多出一条永远为 0 的线）。

**注册表是进程级的全局单例**：模块只在首次 import 时定义一次指标，之后所有请求共享同一组
计数器。多 worker 部署时每个进程各有一份注册表，``/metrics`` 只能看到打到它的那个进程的数值，
需要聚合时应由 Prometheus 按实例抓取后再求和。

【指标清单】名字与标签和 ``docs/提升方案/03-AI能力增强与评测.md`` 第 6.1 节保持一致：

- ``rag_http_requests_total``：HTTP 请求数，标签 ``route`` / ``status``；
- ``rag_retrieval_latency_seconds``：检索耗时，标签 ``mode``；
- ``rag_retrieval_results``：单次检索返回的命中条数；
- ``rag_llm_first_token_seconds``：首个 token 延迟，标签 ``model``；
- ``rag_llm_total_seconds``：整段回答耗时，标签 ``model``；
- ``rag_llm_tokens_total``：token 用量，标签 ``type``（prompt / completion）；
- ``rag_llm_fallback_total``：降级次数，标签 ``reason``；
- ``rag_cache_hits_total``：缓存命中次数，标签 ``kind``（exact / semantic / embedding）；
- ``rag_pipeline_pending_chunks``：待向量化的 active chunk 数（定时从 MySQL 刷）。
"""
from prometheus_client import Counter, Gauge, Histogram

# ---------- 分桶 ----------
# 检索：本机 BGE + Qdrant 通常是几十毫秒；上限放到 10s，覆盖冷启动与 Qdrant 抖动。
_RETRIEVAL_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# 首 token：本地小模型常见 0.3–3s，云端可能到 10s 以上；上限 60s 与 LLM 超时对齐。
_FIRST_TOKEN_BUCKETS = (
    0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 60.0,
)

# 总耗时：包含检索 + 完整生成；上限 120s（默认 LLM 超时 60s，留一倍余量）。
_LLM_TOTAL_BUCKETS = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0, 60.0, 90.0, 120.0,
)

# 命中条数：top_k 通常 5–10；分桶覆盖 0（拒答前的空召回）到 100（评测放大候选）。
_RESULT_BUCKETS = (0, 1, 2, 3, 4, 5, 8, 10, 15, 20, 30, 50, 100)

# ---------- 指标定义 ----------
_HTTP_REQUESTS = Counter(
    "rag_http_requests_total",
    "HTTP requests handled by the RAG service.",
    ["route", "status"],
)

_RETRIEVAL_LATENCY = Histogram(
    "rag_retrieval_latency_seconds",
    "End-to-end retrieval latency (embedding + recall + MySQL hydration).",
    ["mode"],
    buckets=_RETRIEVAL_BUCKETS,
)

_RETRIEVAL_RESULTS = Histogram(
    "rag_retrieval_results",
    "Number of active hits returned by one retrieval call.",
    buckets=_RESULT_BUCKETS,
)

_LLM_FIRST_TOKEN = Histogram(
    "rag_llm_first_token_seconds",
    "Latency from request start to the first streamed token.",
    ["model"],
    buckets=_FIRST_TOKEN_BUCKETS,
)

_LLM_TOTAL = Histogram(
    "rag_llm_total_seconds",
    "Latency from request start to the done event.",
    ["model"],
    buckets=_LLM_TOTAL_BUCKETS,
)

_LLM_TOKENS = Counter(
    "rag_llm_tokens_total",
    "Estimated LLM tokens, split by prompt and completion.",
    ["type"],
)

_LLM_FALLBACK = Counter(
    "rag_llm_fallback_total",
    "Answers that fell back to the extractive provider.",
    ["reason"],
)

_CACHE_HITS = Counter(
    "rag_cache_hits_total",
    "Cache hits by cache kind (exact QA / semantic / query embedding).",
    ["kind"],
)

_PIPELINE_PENDING_CHUNKS = Gauge(
    "rag_pipeline_pending_chunks",
    "Active chunks whose embedding_status is still pending.",
)


# ---------- helper ----------
def observe_http_request(route: str, status: int) -> None:
    """
    给一次 HTTP 请求计数。

    :param route: 路由模板（例如 ``/api/v1/documents/{document_id}``），不是实际 URL；
        用模板才能把 ``/documents/123`` 这类高基数路径压成同一个 label。
    :param status: HTTP 状态码。
    """
    _HTTP_REQUESTS.labels(route=route, status=str(status)).inc()


def observe_retrieval(mode: str, seconds: float, result_count: int) -> None:
    """
    记录一次检索的耗时与返回条数。

    :param mode: 实际检索模式（``dense`` / ``hybrid``）。
    :param seconds: 检索耗时（秒）。
    :param result_count: 回表后的 active 命中条数。
    """
    _RETRIEVAL_LATENCY.labels(mode=mode).observe(seconds)
    _RETRIEVAL_RESULTS.observe(result_count)


def record_llm_first_token(model: str, seconds: float) -> None:
    """
    记录首 token 延迟。

    :param model: 实际产出答案的模型（extractive 兜底时是 ``extractive``）。
    :param seconds: 从请求开始到首个 token 的秒数。
    """
    _LLM_FIRST_TOKEN.labels(model=model).observe(seconds)


def record_llm_total(model: str, seconds: float) -> None:
    """
    记录整段回答（到 ``done``）的耗时。

    :param model: 实际产出答案的模型。
    :param seconds: 从请求开始到生成结束的秒数。
    """
    _LLM_TOTAL.labels(model=model).observe(seconds)


def record_llm_tokens(prompt_tokens: int, completion_tokens: int) -> None:
    """
    记录一次问答的 token 用量（当前是按字符估算值）。

    :param prompt_tokens: prompt 侧 token 数。
    :param completion_tokens: completion 侧 token 数。
    """
    _LLM_TOKENS.labels(type="prompt").inc(prompt_tokens)
    _LLM_TOKENS.labels(type="completion").inc(completion_tokens)


def record_llm_fallback(reason: str) -> None:
    """
    记录一次降级。

    :param reason: 降级原因；当前只有 ``llm_failed``（首 token 前失败）。
    """
    _LLM_FALLBACK.labels(reason=reason).inc()


def record_cache_hit(kind: str) -> None:
    """
    记录一次缓存命中。

    :param kind: 缓存种类；当前用到 ``exact``（精确 QA 缓存）与 ``embedding``
        （query embedding 缓存）。``semantic`` 预留给未上线的语义缓存。
    """
    _CACHE_HITS.labels(kind=kind).inc()


def set_pipeline_pending_chunks(count: int) -> None:
    """
    更新「待向量化 chunk 数」Gauge。

    :param count: 当前 ``superseded_at IS NULL AND embedding_status='pending'`` 的行数。
    """
    _PIPELINE_PENDING_CHUNKS.set(count)
