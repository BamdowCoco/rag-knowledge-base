"""统计出参。"""
from pydantic import BaseModel


class SourceCount(BaseModel):
    source: str
    count: int


class ChunkStatusCount(BaseModel):
    """分块的状态分布 —— pending 那部分就是「向量化积压」。"""
    status: str
    count: int


class StatsOut(BaseModel):
    total: int
    by_source: list[SourceCount]   # 按来源 top10（不含 bench 压测数据）
    today_count: int               # 今日新增
    dedup_rejected: int            # 重复同步拦截累计（syncer 周期重推也会增加；不是错误）
    chunk_status: list[ChunkStatusCount]   # 分块状态分布（pending = 向量化积压）
    bench_count: int = 0           # bench 压测数据单独计数，默认不混入 by_source


class CrawlerStatsOut(BaseModel):
    """爬虫最近一轮的状态 —— 直接读 Redis 里的 ``crawl:*`` 键。"""

    last_run: str | None = None    # 上次完整扫描完成时间（ISO）；还没跑完过则为 None
    seen: int = 0                  # 已推送过的 external_id 数量（``crawl:seen`` 集合大小）
    stats: dict[str, int] = {}     # 本轮计数（fetched / pushed / errors …）；每轮开始会重置


class LlmUsageWindow(BaseModel):
    """一个统计窗口（今日或累计）的 LLM 使用量；token 是 tiktoken 估算值。"""

    requests: int = 0              # 所有问答请求（含缓存/拒答）
    llm_calls: int = 0             # 真正调用 LLM 的次数
    extractive_calls: int = 0      # 走检索片段模式的次数（含降级）
    degraded_calls: int = 0        # LLM 失败后降级的次数
    cache_hits: int = 0            # 精确 QA 缓存命中
    refusals: int = 0              # 资料不足直接拒答
    priced_calls: int = 0          # 命中价格表、已计费的 LLM 调用数
    prompt_tokens: int = 0         # 输入 token
    completion_tokens: int = 0     # 输出 token
    total_tokens: int = 0
    estimated_cost: float | None = None   # 未知模型/无已计价调用时为 None


class LlmUsageOut(BaseModel):
    """LLM 使用量面板的完整出参：当前模型/官方价格 + 今日与累计窗口。"""

    provider: str
    model: str
    currency: str
    pricing_source: str
    pricing_retrieved_at: str
    pricing_period: str | None = None      # 当前 UTC 时段：peak / off-peak
    price_input_cache_hit_per_1m: float | None = None
    price_input_cache_miss_per_1m: float | None = None
    price_output_per_1m: float | None = None
    total: LlmUsageWindow
    today: LlmUsageWindow
