"""纯单测：不启动 MySQL / Qdrant，也不依赖 Redis。

pytest 会用本文件里的同名 fixture 覆盖 ``tests/conftest.py`` 的根 fixture，
所以跑 ``tests/unit`` 时不会触发建库 / 建集合。
"""
import pytest


@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    """覆盖根 conftest：纯单测不建测试库。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def prepare_qdrant():
    """覆盖根 conftest：纯单测不建 Qdrant 集合。"""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """覆盖根 conftest：纯单测不需要清库 / 清 Redis。"""
    yield
