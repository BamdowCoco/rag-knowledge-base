"""
LLM 价格表与费用估算（DeepSeek 中文官方价，2026-11-05 抓取）。

**为什么不用固定 ``.env`` 单价：** 价格随模型和时段（peak / off-peak）变化，
真正可用的输入是 LLM 响应里的 ``model`` 与 ``usage``（含 cache hit / miss token）。
这里用响应里的模型查官方价格表，再按请求发生时的 **北京时间** 时段计算。

**价格来源：** ``https://api-docs.deepseek.com/zh-cn/quick_start/pricing`` ；
表里的数字是该页在 2026-11-05 的 DeepSeek Flash / V4 Pro 价格（**人民币 CNY / 1M tokens**）。
价格表更新时同步修改本模块，并在运维页显示 ``PRICING_RETRIEVED_AT``。

**节假日近似：** 官网说中国法定节假日按 off-peak；本模块只按北京时间周一至周五判断，
节假日会按 peak 计（略微高估）。不影响 token 统计，只影响费用估算。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.utils.time import BEIJING_TZ

logger = logging.getLogger(__name__)

PRICING_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"
PRICING_RETRIEVED_AT = "2026-11-05"
CURRENCY = "CNY"

# 每个模型：cache hit input / cache miss input / output，单位为 **人民币元 / 1M tokens**。
# 每项是 (off-peak, peak)；peak 是 off-peak 的两倍。
_PRICES: dict[str, dict[str, tuple[float, float]]] = {
    "deepseek-flash": {
        "cache_hit": (0.02, 0.04),
        "cache_miss": (1.0, 2.0),
        "output": (4.0, 8.0),
    },
    "deepseek-v4-pro": {
        "cache_hit": (0.15, 0.30),
        "cache_miss": (4.5, 9.0),
        "output": (13.5, 27.0),
    },
}

# 旧模型名仍被厂商接受，计费按 Flash；这里统一映射到当前价格表。
_ALIASES = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}


def canonical_model(model: str | None) -> str | None:
    """
    把厂商返回的模型名映射到价格表里的规范名。

    :param model: 响应或配置里的模型名。
    :return: 规范模型名；未知/空返回 ``None``。
    """
    if not model:
        return None
    name = str(model).strip().lower()
    name = _ALIASES.get(name, name)
    return name if name in _PRICES else None


def is_peak(now: datetime | None = None) -> bool:
    """
    判断某个时刻是否处于 DeepSeek 的 peak 时段。

    官网规则（北京时间）：周一至周五 09:00–12:00 与 14:00–18:00；其余（含周末）
    为 off-peak。中国法定节假日官网按 off-peak，本函数未单独维护节假日表，
    节假日会按工作日近似。

    :param now: 待判断时间；``None`` 用当前北京时间。朴素时间按 UTC 解释。
    :return: 是否 peak。
    """
    moment = now or datetime.now(BEIJING_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(BEIJING_TZ)
    if moment.weekday() >= 5:       # 周六 / 周日
        return False
    minutes = moment.hour * 60 + moment.minute
    # 09:00–12:00 / 14:00–18:00（北京时间）
    return (540 <= minutes < 720) or (840 <= minutes < 1080)


def pricing_info(model: str | None, now: datetime | None = None) -> dict[str, Any] | None:
    """
    返回某个模型在当前时段的单价与来源信息。

    :param model: 模型名（支持旧别名）。
    :param now: 用于判断 peak/off-peak 的时间；``None`` 用当前北京时间。
    :return: 价格信息字典；模型不在价格表时返回 ``None``。
    """
    key = canonical_model(model)
    if key is None:
        return None
    peak = is_peak(now)
    index = 1 if peak else 0
    row = _PRICES[key]
    return {
        "model": key,
        "currency": CURRENCY,
        "period": "peak" if peak else "off-peak",
        "price_input_cache_hit_per_1m": row["cache_hit"][index],
        "price_input_cache_miss_per_1m": row["cache_miss"][index],
        "price_output_per_1m": row["output"][index],
        "pricing_source": PRICING_SOURCE,
        "pricing_retrieved_at": PRICING_RETRIEVED_AT,
    }


def estimate_cost(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    now: datetime | None = None,
) -> float | None:
    """
    按响应 usage 与官方价格计算一次调用的估算费用。

    - ``prompt_tokens`` 是总输入（含 cache hit 与 miss）；``cached_tokens`` 是其中命中缓存的输入；
    - 未知模型返回 ``None``（不编造价格）；
    - 结果单位为 ``CURRENCY``，保留 6 位小数。

    :param model: 响应里的模型名。
    :param prompt_tokens: 输入 token 总数。
    :param completion_tokens: 输出 token 数。
    :param cached_tokens: 输入中命中 cache 的 token 数。
    :param now: 用于判断 peak/off-peak 的时间。
    :return: 估算费用；模型无价格表时返回 ``None``。
    """
    info = pricing_info(model, now)
    if info is None:
        return None

    prompt = max(0, int(prompt_tokens))
    completion = max(0, int(completion_tokens))
    cached = min(max(0, int(cached_tokens)), prompt)
    miss = prompt - cached
    cost = (
        cached * info["price_input_cache_hit_per_1m"]
        + miss * info["price_input_cache_miss_per_1m"]
        + completion * info["price_output_per_1m"]
    ) / 1_000_000
    return round(cost, 6)
