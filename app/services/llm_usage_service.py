"""
LLM 使用量的轻量累计（运维页用）。

**为什么用 Redis 而不是 MySQL：** 这是给运维页看的低频计数器，不参与业务真相；
Redis 已经在跑，`HINCRBY` / `HINCRBYFLOAT` 一次请求最多两次往返；丢了最多从零开始。

**统计口径：**

- ``requests``：所有问答请求（含缓存命中、拒答）；
- ``llm_calls``：真正调用 LLM 的次数；
- ``extractive_calls``：走检索片段模式的次数（含 LLM 失败后的降级）；
- ``cache_hits`` / ``refusals`` / ``degraded_calls`` / ``priced_calls``：各自单独计数；
- ``prompt_tokens`` / ``completion_tokens``：优先用 LLM 响应里的实际 usage；
- ``estimated_cost``：按响应模型 + 官方人民币价格表在**北京时间时段**（peak/off-peak）累计；
  未知模型不估算，只累计 token。

**日期口径：** 「今日」按北京时间。Redis 里没有 date 类型，所以今日 key 用
``llm:usage:today:{CURRENCY}:{YYYYMMDD}``，并设置 2 天过期；累计 key 永久保留
（AOF 持久化）。**key 里带币种**：改成人民币后不会把旧的 USD 累计混进来；
旧币种数据不换算、不读取。
"""
import logging
from datetime import datetime
from typing import Any

from redis.asyncio import Redis

from app.core.config import settings
from app.services.llm_pricing import (
    CURRENCY,
    PRICING_RETRIEVED_AT,
    PRICING_SOURCE,
    pricing_info,
)
from app.utils.time import BEIJING_TZ

logger = logging.getLogger(__name__)

TOTAL_KEY = f"llm:usage:total:{CURRENCY}"
TODAY_PREFIX = f"llm:usage:today:{CURRENCY}:"

# 整数计数字段；estimated_cost 单独做浮点处理。
_WINDOW_FIELDS = (
    "requests",
    "llm_calls",
    "extractive_calls",
    "degraded_calls",
    "cache_hits",
    "refusals",
    "priced_calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
)


def today_key() -> str:
    """
    今日统计的 Redis key（北京时间日期）。

    :return: 形如 ``llm:usage:today:CNY:20261105`` 的 key。
    """
    return f"{TODAY_PREFIX}{datetime.now(BEIJING_TZ).strftime('%Y%m%d')}"


def _as_int(value: Any) -> int:
    """
    把 Redis Hash 里的值转成整数；坏数据按 0 处理。

    :param value: ``str`` / ``bytes`` / ``None`` 。
    :return: 非负整数（理论上不会为负）。
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    """
    把 Redis Hash 里的值转成浮点数；坏数据按 0 处理。

    :param value: ``str`` / ``bytes`` / ``None`` 。
    :return: 浮点数。
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _window(raw: dict[str, Any]) -> dict[str, Any]:
    """
    把一个 Redis Hash 转成统一的窗口结构。

    :param raw: ``hgetall`` 的结果。
    :return: 字段齐全的窗口字典（含 ``estimated_cost``）。
    """
    values = {field: _as_int(raw.get(field)) for field in _WINDOW_FIELDS}
    values["estimated_cost"] = (
        _as_float(raw.get("estimated_cost")) if values["priced_calls"] > 0 else None
    )
    return values


async def record_usage(
    redis: Redis | None,
    *,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    degraded: bool = False,
    cache_hit: bool = False,
    refusal: bool = False,
    cost: float | None = None,
) -> None:
    """
    记录一次问答的使用量；best-effort，Redis 故障只记 warning。

    :param redis: 共享 Redis 客户端；``None`` 表示不记录。
    :param model: 实际产出答案的模型（``extractive`` 表示没有调 LLM）。
    :param prompt_tokens: 输入 token 数（优先来自 LLM 响应 usage）。
    :param completion_tokens: 输出 token 数（优先来自 LLM 响应 usage）。
    :param degraded: 是否降级到 extractive。
    :param cache_hit: 是否命中精确 QA 缓存（不再计 token）。
    :param refusal: 是否因资料不足直接拒答（不调 LLM）。
    :param cost: 本次估算费用；``None`` 表示模型未知/不计价。
    """
    if redis is None:
        return

    increments = {"requests": 1}
    if cache_hit:
        increments["cache_hits"] = 1
    elif refusal:
        increments["refusals"] = 1
    elif model == "extractive":
        increments["extractive_calls"] = 1
    else:
        increments["llm_calls"] = 1
        increments["prompt_tokens"] = max(0, int(prompt_tokens))
        increments["completion_tokens"] = max(0, int(completion_tokens))
        increments["total_tokens"] = max(0, int(prompt_tokens)) + max(
            0, int(completion_tokens)
        )
    if degraded:
        increments["degraded_calls"] = 1
    if cost is not None:
        increments["priced_calls"] = 1

    try:
        key_today = today_key()
        pipe = redis.pipeline()
        for key in (TOTAL_KEY, key_today):
            for field, value in increments.items():
                pipe.hincrby(key, field, value)
            if cost is not None:
                pipe.hincrbyfloat(key, "estimated_cost", float(cost))
        pipe.expire(key_today, 2 * 24 * 3600)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001 - 统计不能影响问答
        logger.warning("记录 LLM 使用量失败（忽略）：%s", exc)


async def get_usage(redis: Redis | None) -> dict[str, Any]:
    """
    读取今日与累计窗口，并附当前模型/官方价格信息。

    :param redis: 共享 Redis 客户端。
    :return: 可直接交给 ``LlmUsageOut`` 的字典；Redis 故障时返回全 0。
    """
    info = pricing_info(settings.llm_model)
    empty = {field: 0 for field in _WINDOW_FIELDS}
    empty["estimated_cost"] = None

    result: dict[str, Any] = {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "currency": (info or {}).get("currency", CURRENCY),
        "pricing_source": (info or {}).get("pricing_source", PRICING_SOURCE),
        "pricing_retrieved_at": (info or {}).get(
            "pricing_retrieved_at", PRICING_RETRIEVED_AT
        ),
        "pricing_period": (info or {}).get("period"),
        "price_input_cache_hit_per_1m": (info or {}).get(
            "price_input_cache_hit_per_1m"
        ),
        "price_input_cache_miss_per_1m": (info or {}).get(
            "price_input_cache_miss_per_1m"
        ),
        "price_output_per_1m": (info or {}).get("price_output_per_1m"),
        "total": dict(empty),
        "today": dict(empty),
    }
    if redis is None:
        return result

    try:
        raw_total = await redis.hgetall(TOTAL_KEY)
        raw_today = await redis.hgetall(today_key())
    except Exception as exc:  # noqa: BLE001 - 运维页不能因为 Redis 抖动 500
        logger.warning("读取 LLM 使用量失败（返回空统计）：%s", exc)
        return result

    result["total"] = _window(raw_total)
    result["today"] = _window(raw_today)
    return result
