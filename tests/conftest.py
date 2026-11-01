"""
测试脚手架：真 MySQL（独立测试库）+ 真 Redis（独立 db 号）+ 真 Qdrant（独立集合）+ ASGI 直连。

跑法（在 compose 网络内跑，别在宿主机直连）：
    docker compose --profile test run --rm test

**为什么用真 MySQL / 真 Redis / 真 Qdrant，而不是 SQLite / fakeredis / 内存向量库：**

 - ``claim_pending`` 的 ``FOR UPDATE SKIP LOCKED`` 是 MySQL 专有，SQLite 执行不了
 - 锁的释放走 Lua ``EVAL``，要真 Redis（省掉 fakeredis[lua] 这个额外依赖）
 - 检索与向量写入要验证 payload 过滤、``scroll`` 分页等真实 Qdrant 行为

被测的正是这些，所以脚手架必须说同一种语言。

**配置从哪来：** compose 的 test 服务把 ``DB_NAME=rag_ingest_test``、``REDIS_DB=15``、
``QDRANT_COLLECTION=document_chunks_test`` 灌进环境变量，
而 app 的 ``settings`` / ``engine`` / ``redis_client`` / ``qdrant_client`` 都在 import 时读环境变量 ——
所以测试进程里的 app **本来就指向测试库 / 测试 redis db / 测试集合** ，不需要 ``dependency_overrides``。
"""
import asyncio
import itertools
import os

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.db.init_db import init_models
from app.db.session import engine
from app.infra.qdrant_client import close_qdrant, ensure_collection, qdrant_client
from app.infra.redis_client import redis_client
from app.main import app

# 清库顺序无所谓（会临时关掉外键检查），但列全了才不会漏表。
# 阶段 F 加了 document_source（身份表）—— 漏了它，身份行会跨测试残留，
# 于是下一个测试用同一个 external_id 时会被当成"更新"而不是"新增"。
_TABLES = (
    "document_chunk",
    "document_dedup",
    "document_clean",
    "document_raw",
    "document_source",
)

# 模块级计数器：`make_doc` 用它发唯一 external_id，跨测试也不会重复
_doc_counter = itertools.count()


@pytest.fixture(scope="session", autouse=True)
async def prepare_database():
    """
    session 级、只做一次：建测试库 + 授权 + 建表。

    **建库要用 root：** compose 里的 ``MYSQL_USER`` 只在 ``MYSQL_DATABASE`` （rag_ingest）上有权限；
    而 ``mysql_data`` 是命名卷，``docker-entrypoint-initdb.d`` 不会重跑 —— 所以测试库得自己建。
    """
    root_pw = os.getenv("DB_ROOT_PASSWORD", "")
    root_url = f"mysql+aiomysql://root:{root_pw}@{settings.db_host}:{settings.db_port}/"
    db = settings.db_name
    # 先断开 app 自己的引擎（它指向测试库）—— 否则它占着的连接会挡住下面的 DROP
    await engine.dispose()

    root_engine = create_async_engine(root_url, pool_pre_ping=True)
    async with root_engine.begin() as conn:
        # 【DROP 而不是 IF NOT EXISTS】测试库【有意】不走 Alembic（跑迁移比 create_all 慢，
        # 见 docs/数据库设计.md §10.2）：schema 由模型的 create_all 建，而它只建缺的表、
        # 不会给已有表加列 —— 留着上一版 schema 的库会直接报 `Unknown column`。
        # 测试库本来就是一次性的，干脆每次从零开始建。
        await conn.execute(text(f"DROP DATABASE IF EXISTS `{db}`"))
        await conn.execute(
            text(f"CREATE DATABASE `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        )
        await conn.execute(text(f"GRANT ALL ON `{db}`.* TO '{settings.db_user}'@'%'"))
        await conn.execute(text("FLUSH PRIVILEGES"))
    await root_engine.dispose()

    await init_models()          # 用 app 自己的 engine（已指向测试库）建表

    yield

    await engine.dispose()
    await redis_client.aclose()


@pytest.fixture(scope="session", autouse=True)
async def prepare_qdrant():
    """
    session 级、只做一次：确保测试集合存在（维度 / payload 索引就绪）。

    **为什么不能省：** 测试走 ``ASGITransport`` ，它 **不跑 lifespan** ——
    而生产环境里「建集合」正是 lifespan 干的活。少了这一步，所有写向量 / 检索的测试
    都会撞上 ``Collection not found`` 。

    **先删再建：** 上一次测试跑完后集合可能还在（甚至维度是旧的 512），直接
    ``ensure_collection()`` 会因为维度不一致抛错。测试集合本来就是一次性的，先删干净。

    关掉时释放共享客户端；``_clean`` 每测试重建集合，client 本身可以一直复用。
    """
    try:
        await qdrant_client.delete_collection(settings.qdrant_collection)
    except Exception:  # noqa: BLE001 - 首次运行时集合不存在
        pass

    # compose 的 test 服务只等 qdrant「容器已启动」（镜像里没有 curl/wget 做健康检查）；
    # 容器刚起来时 Qdrant 可能还没 ready —— 测试侧重试几十秒，别让一次启动竞态把整轮打挂。
    last_error: Exception | None = None
    for _ in range(30):
        try:
            await ensure_collection()
            break
        except Exception as exc:  # noqa: BLE001 - 连接未就绪时重试；维度错误也会在 30s 后抛出
            last_error = exc
            await asyncio.sleep(1)
    else:
        assert last_error is not None
        raise last_error

    yield
    await close_qdrant()


@pytest.fixture(autouse=True)
async def _clean(prepare_database, prepare_qdrant):
    """
    每个测试前清干净：TRUNCATE 五张表 + flush 当前 redis db 号 + 重建 Qdrant 集合。

    **为什么不用「事务回滚」隔离：** 应用内部到处 ``await db.commit()``
    （``ingest``、``chunk_repo`` 每次 claim/mark 都提交），外层事务回滚 **撤不掉已提交的写** 。
    改用「建表一次 + 每测试清表」。

    MySQL 不允许 TRUNCATE 被外键引用的表，所以临时关掉外键检查。

    **Qdrant 用「删集合再重建」而不是按点删除：** 测试之间不能残留任何 point
    （尤其 ``document_chunk`` 的 id 会从 1 重新自增，上一测试的点会撞上这一测试的 id）。
    重建一次比逐个按 filter 删除更彻底，也更快。
    """
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in _TABLES:
            await conn.execute(text(f"TRUNCATE TABLE {t}"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    await redis_client.flushdb()   # 只清当前 db 号（15）；生产 db 0 在同一个容器里，不受影响

    try:
        await qdrant_client.delete_collection(settings.qdrant_collection)
    except Exception:  # noqa: BLE001 - 集合本来就不存在（首个测试）时会报 404，忽略即可
        pass
    await ensure_collection()


@pytest.fixture(scope="session")
async def client():
    """
    ASGI 直连的客户端（不走网络，也不需要起 uvicorn）。

    **注意 ``ASGITransport`` 不跑 lifespan** —— 所以：

    - 建表靠上面的 ``prepare_database`` （不是靠 lifespan 里的 ``init_models()``）
    - 建 Qdrant 集合靠 ``prepare_qdrant`` （不是靠 lifespan 里的 ``ensure_collection()``）
    - lifespan 里的 ``reclaim_loop()`` **不会** 启动（回收测试直接调 ``reclaim_once()``）
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def redis():
    """共享的 redis 客户端（与 app 用的是同一个）。"""
    return redis_client


@pytest.fixture
def make_doc():
    """
    造一篇 **内容唯一** 的文档。

    默认带一个 **唯一的 ``external_id``** —— 阶段 F 之后它是必填的，而且 **身份** 才是判「同一篇」的依据。
    计数器放在 **模块级** ：这样一个会话里绝不会重复用到同一个 id（跨测试文件的重复尤其坑）。
    想让两篇文档"是同一篇"，就显式传同一个 ``external_id``。
    """

    def _make(**overrides) -> dict:
        n = next(_doc_counter)
        doc = {
            "source": "feishu",
            "external_id": f"ext-{n}",
            "title": f"标题-{n}",
            "content": f"这是第 {n} 篇的正文内容。",
        }
        doc.update(overrides)
        return doc

    return _make
