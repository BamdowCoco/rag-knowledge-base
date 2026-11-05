"""
LLM provider 注册表（懒加载 ``openai_compat`` + ``extractive`` 单例）。

``extractive`` 只依赖标准库与 ``prompt_builder`` ，可以顶层实例化。这里有两个实例：

- ``EXTRACTIVE``：**主动选择** ``LLM_PROVIDER=extractive`` 时使用，提示语是“检索片段模式”；
- ``EXTRACTIVE_FALLBACK``：LLM 首 token 前失败时使用，提示语是“模型暂不可用”。

两个实例共用同一套片段整理逻辑，但用户看到的文案不同 —— 主动选择不是故障。

``openai_compat`` 依赖共享 httpx 客户端，首次选中时才 import / 实例化。
"""
from .base import LLMError, LLMProvider
from .extractive import DEMO_NOTICE, ExtractiveProvider

# 主动选择 extractive 时的单例（演示模式，不是故障）
EXTRACTIVE = ExtractiveProvider(notice=DEMO_NOTICE)

# LLM 首 token 前失败时的兜底单例（文案是“模型暂不可用”）
EXTRACTIVE_FALLBACK = ExtractiveProvider()

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
    "EXTRACTIVE_FALLBACK",
    "LLMError",
    "LLMProvider",
    "available",
    "get_provider",
    "reset_provider_cache",
]
