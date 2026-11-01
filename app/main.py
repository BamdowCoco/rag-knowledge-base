"""
FastAPI 入口

lifespan 是「应用启动到关闭」的生命周期钩子：yield 之前是启动逻辑，yield 之后是关停逻辑。

**同一个镜像按 ``APP_ROLE`` 扮演不同进程**：

- ``ingest`` / ``all``：探活 Redis、把库升到最新、起回收与 enrich 循环；
- ``rag`` / ``all``：确保 Qdrant 集合存在（维度 / payload 索引就绪）；
- ``rag``：不探活 Redis、不迁移、不起后台循环 —— 它只读，Redis 只是缓存，
  缓存故障在服务内吞掉，不能拦住问答启动。
"""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.db.session import engine
from app.infra.ai_client import close_ai_clients
from app.infra.model_client import close_model_client
from app.infra.qdrant_client import close_qdrant, ensure_collection
from app.infra.redis_client import close_redis, redis_client
from app.services.enricher import enrich_loop
from app.services.reclaimer import reclaim_loop

setup_logging()  # 必须在其它模块打日志之前调用
logger = logging.getLogger(__name__)

# alembic.ini 在仓库根（app/ 的上一层）—— 用绝对路径，不依赖启动时的 CWD
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _upgrade_schema() -> None:
    """
    把库升到最新一版（alembic 是同步 API，调用方要丢到线程里跑）。

    换成迁移之后， **加列不再等于"重建库、丢数据"** —— 空库首启它会把五张表建出来，
    以后改 schema 就写一条迁移升上去。这正是前几轮反复踩的那个坑。

    多 worker 启动时由 ``alembic/env.py`` 的咨询锁串行化，不会并发 DDL。
    """
    command.upgrade(AlembicConfig(str(_ALEMBIC_INI)), "head")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    应用生命周期：按角色准备依赖，关停时反序释放。

    :param app: FastAPI 应用实例（由框架传入）。
    yield 之前是启动逻辑，之后是关停逻辑。
    """
    role = settings.app_role
    background: list[asyncio.Task[None]] = []

    # ---------- 启动 ----------
    logger.info("启动 %s (role=%s, debug=%s)", settings.app_name, role, settings.debug)
    logger.info("MySQL -> %s:%s/%s", settings.db_host, settings.db_port, settings.db_name)

    if role in ("all", "ingest"):
        # 只打 host:port，别把带密码的 database_url 整个打出来
        logger.info("Redis -> %s:%s/%s", settings.redis_host, settings.redis_port, settings.redis_db)
        await redis_client.ping()   # 探活：Redis 不可用就在启动阶段直接失败（fail fast）
        logger.info("Redis 连接正常")

        # 迁移是阻塞的（连库 + 执行 DDL），所以丢到线程里，别卡住事件循环
        await asyncio.to_thread(_upgrade_schema)
        logger.info("数据库表已就绪")

        # 两个应用内的循环，职责不同、形状一样（都是 lifespan 起停 + 每轮吞异常）：
        #   reclaimer —— 把"卡住的"放回去（chunk 的 processing、文档的 enrich processing）
        #   enricher  —— 把"待办的"做掉（模型调用是秒级外部依赖，不能压在请求路径里）
        background = [
            asyncio.create_task(reclaim_loop()),
            asyncio.create_task(enrich_loop()),
        ]
        logger.info("后台循环已启动：reclaim / enrich")
    else:
        # rag 只读：不迁移、不起后台循环、也不要求 Redis 可用（缓存故障在服务内吞掉）
        logger.info("rag 角色：跳过 Redis 探活、数据库迁移与后台循环")

    if role in ("all", "rag"):
        # 集合维度与 EMBEDDING_DIM 一致才能写/查；不一致直接启动失败，别等第一个请求
        await ensure_collection()
        logger.info("Qdrant 集合就绪：%s", settings.qdrant_collection)

    yield

    # ---------- 关停 ----------
    # 顺序要紧：先取消后台任务、再关连接池。反过来的话，任务可能拿到已 dispose 的池。
    for task in background:
        task.cancel()
    for task in background:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await close_ai_clients()    # embedding / LLM 两个 HTTP 客户端
    await close_qdrant()        # Qdrant 客户端
    await close_model_client()  # 释放模型服务客户端的连接池
    await close_redis()         # 释放 Redis 连接池
    await engine.dispose()      # 释放数据库连接池
    logger.info("已关闭 %s", settings.app_name)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
register_exception_handlers(app)     # 业务异常 → HTTP 的统一映射在这里挂上
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """
    健康检查。

    :return: ``{"status": "ok"}``；能返回就说明进程在正常处理请求。
    """
    return {"status": "ok"}
