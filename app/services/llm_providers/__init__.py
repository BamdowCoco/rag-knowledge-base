"""
LLM provider 注册表（懒加载 ``openai_compat`` + ``extractive`` 单例）。

``extractive`` 只依赖标准库与 ``prompt_builder`` ，可以顶层实例化 ——
它同时是"LLM 首 token 前失败"时的降级终点，``answer_service`` 会直接引用 ``EXTRACTIVE`` 。
``openai_compat`` 依赖共享 httpx 客户端，首次选中时才 import / 实例化。
"""
from .base import LLMError, LLMProvider
from .extractive import ExtractiveProvider

# 降级链的终点单例 —— answer_service 首 token 前失败时直接用它
EXTRACTIVE = ExtractiveProvider()

# 已实例化的 provider 缓存（extractive 一开始就在里面）
_CACHE: dict[str, LLMProvider] = {"extractive": EXTRACTIVE}

_NAMES = ("openai_compat", "extractive")


def get_provider(name: str) -> LLMProvider:
    """
    按名字取 provider。

    :param name: ``openai_compat`` / ``extractive``。
    :return: 进程内共享的 provider 实例。
    :raises ValueError: 名字不在注册表里。
    """
    provider = _CACHE.get(name)
    if provider is not None:
        return provider

    if name == "openai_compat":
        from .openai_compat import OpenAICompatLLMProvider

        provider = OpenAICompatLLMProvider()
        _CACHE[name] = provider
        return provider

    raise ValueError(f"未知的 LLM provider {name!r}；可选：{', '.join(_NAMES)}")


def reset_provider_cache() -> None:
    """
    清空实例缓存（保留 ``EXTRACTIVE`` 单例；测试改 ``settings`` 后调用）。
    """
    for name in list(_CACHE):
        if name != "extractive":
            del _CACHE[name]


def available() -> list[str]:
    """
    有哪些 provider 可选 —— 给排错与文档用。

    :return: 按字母序排好的 provider 名。
    """
    return sorted(_NAMES)


__all__ = [
    "EXTRACTIVE",
    "LLMError",
    "LLMProvider",
    "available",
    "get_provider",
    "reset_provider_cache",
]
