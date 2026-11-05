"""爬虫测试：不需要 MySQL / Qdrant；有状态测试只依赖 Redis。"""
import pytest

from app.infra.redis_client import redis_client


@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    """覆盖根 conftest：爬虫测试不需要 MySQL。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def prepare_qdrant():
    """覆盖根 conftest：爬虫测试不需要 Qdrant。"""
    yield


@pytest.fixture(autouse=True)
async def _clean():
    """每个测试前清空当前 Redis db（测试专用 db 15）。"""
    await redis_client.flushdb()
