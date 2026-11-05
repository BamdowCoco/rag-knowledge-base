"""
FastAPI 入口

lifespan 是「应用启动到关闭」的生命周期钩子：yield 之前是启动逻辑，yield 之后是关停逻辑。

**同一个镜像按 ``APP_ROLE`` 扮演不同进程**：

- ``ingest`` / ``all``：探活 Redis、把库升到最新、起回收与 enrich 循环；
- ``rag`` / ``all``：确保 Qdrant 集合存在（维度 / payload 索引就绪）、挂 ``/metrics``；
- ``rag``：不探活 Redis、不迁移、不起摄入侧后台循环 —— 它只读，Redis 只是缓存，
  缓存故障在服务内吞掉，不能拦住问答启动。它仍会起「待向量化 chunk 数」的指标刷新循环，
  因为那只是只读查 MySQL。
"""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.core.metrics import observe_http_request
from app.db.session import engine
from app.infra.ai_client import close_ai_clients
from app.infra.model_client import close_model_client
from app.infra.qdrant_client import close_qdrant, ensure_collection
from app.infra.redis_client import close_redis, redis_client
from app.services.enricher import enrich_loop
from app.services.metrics_refresh import metrics_loop
from app.services.reclaimer import reclaim_loop

setup_logging()  # 必须在其它模块打日志之前调用
logger = logging.getLogger(__name__)

# alembic.ini 在仓库根（app/ 的上一层）—— 用绝对路径，不依赖启动时的 CWD
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

# 演示前端目录（仓库根的 ``web/``）—— 同样用绝对路径，容器里是 ``/app/web``。
# 本地开发没写前端时目录可能不存在，挂载前会判断。
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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

        # 三个应用内的循环，职责不同、形状一样（都是 lifespan 起停 + 每轮吞异常）：
        #   reclaimer —— 把"卡住的"放回去（chunk 的 processing、文档的 enrich processing）
        #   enricher  —— 把"待办的"做掉（模型调用是秒级外部依赖，不能压在请求路径里）
        #   metrics   —— 定时把待向量化 chunk 数刷进 Gauge（只读查库，不参与业务判断）
        background = [
            asyncio.create_task(reclaim_loop()),
            asyncio.create_task(enrich_loop()),
            asyncio.create_task(metrics_loop()),
        ]
        logger.info("后台循环已启动：reclaim / enrich / metrics")
    else:
        # rag 只读：不迁移、不要求 Redis 可用（缓存故障在服务内吞掉）、不起摄入侧循环。
        # 指标刷新只读查 MySQL，所以 rag 也起一个 —— 两个角色暴露同一套积压数。
        background = [asyncio.create_task(metrics_loop())]
        logger.info("rag 角色：跳过 Redis 探活、数据库迁移与摄入循环；仅启动 metrics 刷新")

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


@app.middleware("http")
async def count_http_requests(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """
    按 ``(route, status)`` 给 HTTP 请求计数。

    **route 优先取路由模板**（``request.scope["route"].path``，例如
    ``/api/v1/documents/{document_id}``）；取不到模板时才退回实际 URL —— 直接拿 URL 当
    label，会把 ``/documents/123``、``/documents/124`` 打成上千条各占一条的时间序列。

    ``call_next`` 返回的是响应对象，对 SSE 而言它表示「响应头已就绪」，不是「流已写完」；
    请求数按『已开始处理』计一次即可，不追求字节级完成时间。

    未处理异常会由最外层的 ``ServerErrorMiddleware`` 变成 500；这里捕获后先记一笔再原样
    抛出，否则错误率面板会漏掉真正把请求打挂的那些调用。

    :param request: 入站请求。
    :param call_next: 交给下游处理并取回响应。
    :return: 下游响应，原样返回（不改状态码与响应体）。
    """
    route = request.scope.get("route")
    route_path = getattr(route, "path", None) or request.url.path
    try:
        response = await call_next(request)
    except Exception:
        observe_http_request(route_path, 500)
        raise
    observe_http_request(route_path, response.status_code)
    return response


@app.middleware("http")
async def no_cache_static_assets(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """
    给演示用静态页/资源加 ``Cache-Control: no-cache``。

    ``no-cache`` 不是不缓存，而是“每次先回源校验”。本地 demo 改完前端后，
    浏览器不会继续拿旧 ``admin.js`` 把已经排除的 bench 又画出来。
    只对非 ``/api/`` 路径生效，接口缓存策略不受影响。

    :param request: 入站请求。
    :param call_next: 下游处理。
    :return: 原响应（仅补一个缓存头）。
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# CORS 白名单：运维页跑在 rag-api 的静态页上（:8001），统计接口在 api（:8000），
# 跨源调用要靠它放行。**注册放在最后** —— ``add_middleware`` 后加的更靠外层，
# 这样预检请求在计数中间件之前就被 CORSMiddleware 直接应答，不会混进业务请求计数。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,   # 具体白名单，不用 "*"
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,                   # 纯白名单来源，不带 cookie，无需凭据
)


if settings.app_role in ("all", "rag"):
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        """
        Prometheus 文本格式的指标快照。

        挂在顶层而不是 ``/api/v1`` 下：抓取端按约定直接请求 ``/metrics``，也不该被 API
        的版本化节奏牵着走。``ingest`` 角色不挂 —— 它没有检索 / 问答指标。

        :return: ``text/plain; version=0.0.4`` 格式的指标文本。
        """
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """
    健康检查。

    :return: ``{"status": "ok"}``；能返回就说明进程在正常处理请求。
    """
    return {"status": "ok"}


# ---------- 演示前端（静态页） ----------
# **必须挂在所有具体路由之后**：``mount("/")`` 是兜底匹配，Starlette 按注册顺序找路由，
# 排在前面会把 ``/api/v1/*``、``/health``、``/metrics`` 一起吃掉（静态目录里找不到就 404）。
# 只在 ``all`` / ``rag`` 角色挂 —— 和问答接口的可见范围一致；``ingest`` 是纯写入进程。
if settings.app_role in ("all", "rag") and _WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
    logger.info("演示前端已挂载：%s", _WEB_DIR)
