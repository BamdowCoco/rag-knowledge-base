"""基础设施测试：只需要 Redis，不启动 MySQL / Qdrant。"""
import pytest

from app.infra.redis_client import redis_client


@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    """覆盖根 conftest：锁测试不需要 MySQL。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def prepare_qdrant():
    """覆盖根 conftest：锁测试不需要 Qdrant。"""
    yield


@pytest.fixture(autouse=True)
async def _clean():
    """每个测试前清空当前 Redis db（测试专用 db 15）。"""
    await redis_client.flushdb()
