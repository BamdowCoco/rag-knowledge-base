"""官方价格表：peak/off-peak、模型别名与 cache hit/miss 费用。"""
from datetime import datetime, timezone

from app.services.llm_pricing import (
    canonical_model,
    estimate_cost,
    is_peak,
    pricing_info,
)


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """
    造一个 UTC 时间。

    :return: 带 UTC 时区的 datetime。
    """
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_peak_windows_are_beijing_weekdays_only():
    """北京时间周一至周五 09–12、14–18 为 peak；周末与其余时段 off-peak。"""
    assert is_peak(_utc(2026, 11, 2, 1)) is True       # 北京周一 09:00
    assert is_peak(_utc(2026, 11, 2, 2)) is True       # 北京周一 10:00
    assert is_peak(_utc(2026, 11, 2, 4)) is False      # 北京周一 12:00（不含端点）
    assert is_peak(_utc(2026, 11, 2, 5)) is False      # 北京周一 13:00
    assert is_peak(_utc(2026, 11, 2, 7)) is True       # 北京周一 15:00
    assert is_peak(_utc(2026, 11, 2, 10)) is False     # 北京周一 18:00（不含端点）
    assert is_peak(_utc(2026, 11, 7, 2)) is False      # 北京周六 10:00
    assert is_peak(_utc(2026, 11, 1, 17)) is False     # 北京周一 01:00（跨日）


def test_model_aliases_resolve_to_current_names():
    assert canonical_model("deepseek-v4-flash") == "deepseek-flash"
    assert canonical_model("deepseek-v4-flash-vision-exp") == "deepseek-flash"
    assert canonical_model("deepseek-flash") == "deepseek-flash"
    assert canonical_model("unknown-model") is None


def test_pricing_info_uses_peak_and_off_peak_rates():
    peak = pricing_info("deepseek-flash", _utc(2026, 11, 2, 2))
    off = pricing_info("deepseek-flash", _utc(2026, 11, 2, 5))

    assert peak is not None and off is not None
    assert peak["period"] == "peak"
    assert off["period"] == "off-peak"
    assert peak["currency"] == "CNY"
    assert peak["price_input_cache_hit_per_1m"] == 0.04
    assert peak["price_input_cache_miss_per_1m"] == 2.0
    assert peak["price_output_per_1m"] == 8.0
    assert off["price_input_cache_hit_per_1m"] == 0.02
    assert off["price_input_cache_miss_per_1m"] == 1.0
    assert off["price_output_per_1m"] == 4.0


def test_estimate_cost_splits_cache_hit_and_miss():
    """1M 输入（其中 1M 全部命中缓存）+ 1M 输出，按 off-peak Flash 价。"""
    off_peak = _utc(2026, 11, 2, 5)

    all_cached = estimate_cost(
        "deepseek-flash", 1_000_000, 1_000_000, cached_tokens=1_000_000, now=off_peak
    )
    all_miss = estimate_cost(
        "deepseek-flash", 1_000_000, 1_000_000, cached_tokens=0, now=off_peak
    )

    assert all_cached == round(0.02 + 4.0, 6)
    assert all_miss == round(1.0 + 4.0, 6)


def test_estimate_cost_returns_none_for_unknown_model():
    """未知模型不估算费用，避免编造价格。"""
    assert estimate_cost("unknown-model", 1000, 1000) is None
