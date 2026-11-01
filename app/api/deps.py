"""
依赖注入：把 db / redis / 外部客户端 / service 组装起来。
endpoint 只写 ``service: XxxService = Depends(get_xxx_service)``，不碰 session/redis。

【为什么 provider 用函数包一层】注册表里的 ``get_provider`` 按名字取实例；
依赖注入要的是「无参可调用」。包一层之后测试也能 ``app.dependency_overrides[get_llm_provider]``
把假 provider 换进来，不必去改全局注册表。
"""
from typing import Any

from fastapi import Depends
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.infra.qdrant_client import get_qdrant
from app.infra.redis_client import get_redis
from app.services.answer_service import AnswerService
from app.services.document_service import DocumentService
from app.services.retrieval_service import RetrievalService
from app.services.task_service import TaskService


def get_document_service(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> DocumentService:
    """
    组装文档服务。

    :param db: 请求级数据库会话。
    :param redis: 共享 Redis 客户端。
    :return: 绑定好会话与 Redis 的 ``DocumentService``。
    """
    return DocumentService(db, redis)


def get_task_service(db: AsyncSession = Depends(get_db)) -> TaskService:
    """
    组装任务服务。

    :param db: 请求级数据库会话。
    :return: 绑定好会话的 ``TaskService``。任务分发只用得到数据库
        （``SKIP LOCKED`` 是 MySQL 的能力），不需要 Redis。
    """
    # 任务分发只用得到数据库（SKIP LOCKED 是 MySQL 的能力），不需要 redis
    return TaskService(db)


def get_qdrant_client() -> AsyncQdrantClient:
    """
    取共享的 Qdrant 客户端。

    :return: 模块级单例 ``AsyncQdrantClient``。
    """
    return get_qdrant()


def get_embedding_provider() -> Any:
    """
    按 ``EMBEDDING_PROVIDER`` 取 embedding provider（注册表内部懒加载 + 单例）。

    :return: embedding provider 实例。
    """
    # 别名导入：本模块的依赖函数也叫 get_embedding_provider，避免同名遮蔽
    from app.services.embedding_providers import get_provider as get_embedding_provider_impl

    return get_embedding_provider_impl(settings.embedding_provider)


def get_llm_provider() -> Any:
    """
    按 ``LLM_PROVIDER`` 取 LLM provider（``openai_compat`` / ``extractive``）。

    :return: LLM provider 实例。
    """
    from app.services.llm_providers import get_provider as get_llm_provider_impl

    return get_llm_provider_impl(settings.llm_provider)


def get_retrieval_service(
    db: AsyncSession = Depends(get_db),
    qdrant: AsyncQdrantClient = Depends(get_qdrant_client),
    redis: Redis = Depends(get_redis),
    provider: Any = Depends(get_embedding_provider),
) -> RetrievalService:
    """
    组装检索服务。

    :param db: 请求级数据库会话（回表校验 active 版本）。
    :param qdrant: 共享 Qdrant 客户端。
    :param redis: 共享 Redis 客户端（query embedding 缓存）。
    :param provider: embedding provider。
    :return: 绑定好各依赖的 ``RetrievalService``。
    """
    return RetrievalService(db, qdrant, provider, redis)


def get_answer_service(
    retrieval: RetrievalService = Depends(get_retrieval_service),
    redis: Redis = Depends(get_redis),
    llm: Any = Depends(get_llm_provider),
) -> AnswerService:
    """
    组装问答服务。

    通过 ``Depends(get_llm_provider)`` 取 provider，测试可直接 override 这个名字。

    :param retrieval: 检索服务。
    :param redis: 共享 Redis 客户端（精确 QA 缓存）。
    :param llm: LLM provider。
    :return: 绑定好各依赖的 ``AnswerService``。
    """
    return AnswerService(retrieval, llm, redis)
