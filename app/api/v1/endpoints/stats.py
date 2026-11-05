"""统计接口：总数 / 按来源 top10 / 今日新增 / 判重拒收累计 / 爬虫最近一轮。"""
import logging

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.api.deps import get_document_service
from app.infra.redis_client import get_redis
from app.schemas.stats import CrawlerStatsOut, LlmUsageOut, StatsOut
from app.services.document_service import DocumentService
from app.services.llm_usage_service import get_usage

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/stats", response_model=StatsOut, summary="文档统计")
async def get_stats(service: DocumentService = Depends(get_document_service)) -> StatsOut:
    """
    文档统计（整体缓存，与列表共用版本号）。

    :param service: 文档服务。
    :return: 总数 / 来源 top10 / 今日新增 / 判重拒收 / chunk 状态分布。
    """
    return StatsOut(**await service.get_stats())


@router.get(
    "/crawler-stats",
    response_model=CrawlerStatsOut,
    summary="爬虫最近一轮统计",
)
async def get_crawler_stats(redis: Redis = Depends(get_redis)) -> CrawlerStatsOut:
    """
    爬虫最近一轮的状态（直接读 Redis 里的 ``crawl:*`` 键）。

    **这是「最近一轮」不是累计：** ``crawl:stats`` 在每轮开始时会被 ``start_run()``
    清空，所以它要么是正在跑的这一轮，要么是刚跑完、还没开下一轮的那一轮。
    已推送过的 URL 去重计数 ``crawl:seen`` 则是跨轮保留的累计集合大小。

    运维页不能因为 Redis 抖动整页打挂：读失败时只记 warning，返回空统计。

    :param redis: 共享 Redis 客户端。
    :return: 上次完整扫描时间 / 已见 URL 数 / 本轮计数。
    """
    try:
        raw_stats = await redis.hgetall("crawl:stats")
        last_run = await redis.get("crawl:last_run")
        seen = await redis.scard("crawl:seen")
        stats = {str(key): int(value) for key, value in raw_stats.items()}
    except Exception:  # noqa: BLE001 - 缓存故障不能 500 打挂运维页
        logger.warning("读取爬虫统计失败（Redis 不可用？），返回空统计", exc_info=True)
        return CrawlerStatsOut()

    return CrawlerStatsOut(last_run=last_run, seen=seen, stats=stats)


@router.get(
    "/llm-usage",
    response_model=LlmUsageOut,
    summary="LLM 使用量（今日 / 累计）",
)
async def get_llm_usage(redis: Redis = Depends(get_redis)) -> LlmUsageOut:
    """
    LLM 使用量：调用次数、降级/缓存命中、token 估算与费用。

    **费用是估算值：** token 由 tiktoken 估算，费用按 DeepSeek 中文官方页的
    人民币单价（元 / 1M tokens）与请求时的北京 peak/off-peak 时段计算；
    模型不在价格表时 ``estimated_cost`` 为 ``None``，运维页显示“未计价”。

    :param redis: 共享 Redis 客户端。
    :return: 今日 / 累计窗口 + 当前 provider / model / 单价。
    """
    return LlmUsageOut(**await get_usage(redis))
