"""
AI 上游的 HTTP 客户端：embedding 与 LLM 各一个 ``httpx.AsyncClient``。

完全照 ``model_client.py`` 的形状： **模块级单例 + lifespan 关停时释放连接池** 。
别每次调用新建客户端 —— 那样每个请求都要重新建连，连接池就白搭了。

【为什么分成两个】两者的超时差一个数量级：embedding 是秒级批处理（``EMBEDDING_TIMEOUT_SECONDS``），
LLM 流式生成可能要几十秒（``LLM_TIMEOUT_SECONDS``）。共用一个客户端只能取其一，必有一边不合适。

【构造时不连接】``httpx.AsyncClient`` 构造不建立任何连接，所以 import 本模块不会拖慢启动；
真正连上游是首次 ``post`` / ``stream`` 时。
"""
import httpx

from app.core.config import settings

# embedding 端点：批量向量化，超时短（默认 10s），失败就交给重试 / 退避
embedding_client: httpx.AsyncClient = httpx.AsyncClient(
    timeout=settings.embedding_timeout_seconds
)

# LLM 端点：流式生成，超时长（默认 60s），按"整段读超时"算
llm_client: httpx.AsyncClient = httpx.AsyncClient(timeout=settings.llm_timeout_seconds)


def get_embedding_client() -> httpx.AsyncClient:
    """
    给 embedding provider 拿共享客户端。

    :return: 模块级单例 ``httpx.AsyncClient``。
    """
    return embedding_client


def get_llm_client() -> httpx.AsyncClient:
    """
    给 LLM provider 拿共享客户端。

    :return: 模块级单例 ``httpx.AsyncClient``。
    """
    return llm_client


async def close_ai_clients() -> None:
    """
    关停时释放两个连接池（由 lifespan 调用）。
    """
    await embedding_client.aclose()
    await llm_client.aclose()
