"""
embedding provider 注册表（ **懒加载 + 实例缓存** ）。

【为什么必须懒加载】``fastembed`` 依赖 ``onnxruntime`` ，只装在 ``rag-api`` / ``embedder`` 镜像里；
ingest API 与 test 镜像用 ``hash`` 桩，根本没装它。所以 ``get_provider`` 只在 **选中哪个才 import 哪个** ——
本模块顶层不能出现 ``from .fastembed_provider import ...`` ，否则 ingest 镜像 import 注册表就炸。

【实例缓存】``fastembed`` 加载模型要秒级，``openai_compat`` 至少要复用共享 httpx 客户端；
provider 建成后放 ``_CACHE`` ，每进程只建一次。测试改了 ``settings`` 后调 ``reset_provider_cache()`` 清理。

【名字从哪来】``settings.embedding_provider`` 用的是 ``Literal`` —— 写错值 **启动就报错** ；
这里的 ``get_provider`` 仍对未知名字抛 ``ValueError`` ，方便有人绕过配置直接调用时暴露问题。
"""
import threading

from .base import EmbeddingError, EmbeddingProvider

# provider 名 -> 实例。缓存的不只是对象，还有 fastembed 已经加载好的模型
_CACHE: dict[str, EmbeddingProvider] = {}

# uvicorn 多 worker 是进程隔离的，但同一进程里可能有多线程初始化（测试 / 后台任务）；
# 加锁避免把模型重复加载两遍
_LOCK = threading.Lock()

# 注册表里可选的名字（available() 与文档共用，别散落成多处字面量）
_NAMES = ("hash", "fastembed", "openai_compat")


def get_provider(name: str) -> EmbeddingProvider:
    """
    按名字取 provider（首次调用时 import 对应模块并实例化）。

    :param name: ``hash`` / ``fastembed`` / ``openai_compat``。
    :return: 进程内共享的 provider 实例。
    :raises ValueError: 名字不在注册表里。
    """
    provider = _CACHE.get(name)
    if provider is not None:
        return provider

    with _LOCK:
        # 双重检查：等锁期间可能已有别的线程建好了
        provider = _CACHE.get(name)
        if provider is not None:
            return provider

        if name == "hash":
            from .hash import HashProvider

            provider = HashProvider()
        elif name == "fastembed":
            # 只有这里才会 import onnxruntime 那一套。
            # 模块名用 fastembed_provider，别用 fastembed.py —— 避免和第三方包同名，
            # 否则 IDE / 某些 sys.path 场景会把 `from fastembed import ...` 指回自己。
            from .fastembed_provider import FastEmbedProvider

            provider = FastEmbedProvider()
        elif name == "openai_compat":
            from .openai_compat import OpenAICompatEmbeddingProvider

            provider = OpenAICompatEmbeddingProvider()
        else:
            raise ValueError(
                f"未知的 embedding provider {name!r}；可选：{', '.join(_NAMES)}"
            )

        _CACHE[name] = provider
        return provider


def reset_provider_cache() -> None:
    """
    清空实例缓存（测试改 ``settings`` 后调用；下次 ``get_provider`` 会重新构造）。
    """
    with _LOCK:
        _CACHE.clear()


def available() -> list[str]:
    """
    有哪些 provider 可选 —— 给排错与文档用。

    :return: 按字母序排好的 provider 名。
    """
    return sorted(_NAMES)


__all__ = [
    "EmbeddingError",
    "EmbeddingProvider",
    "available",
    "get_provider",
    "reset_provider_cache",
]
