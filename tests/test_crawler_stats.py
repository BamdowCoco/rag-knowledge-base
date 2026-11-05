"""
爬虫状态接口 ``GET /api/v1/documents/crawler-stats``：从 Redis 的 ``crawl:*`` 键读最近一轮。

键的写法与 ``crawler/state.py`` 保持一致：``crawl:stats``（Hash）、``crawl:last_run``
（String）、``crawl:seen``（Set）。``_clean`` 每个测试前会 flushdb，所以空 Redis 的
用例不依赖测试顺序。
"""
from app.infra.redis_client import get_redis
from app.main import app


async def test_crawler_stats_reads_redis(client, redis):
    """往 Redis 写一轮状态，接口按字段原样返回。"""
    await redis.hset(
        "crawl:stats",
        mapping={"fetched": 12, "not_modified": 3, "pushed": 9, "errors": 1},
    )
    await redis.set("crawl:last_run", "2026-11-01T12:30:00+00:00")
    await redis.sadd("crawl:seen", "https://example.com/a", "https://example.com/b")

    response = await client.get("/api/v1/documents/crawler-stats")

    assert response.status_code == 200
    assert response.json() == {
        "last_run": "2026-11-01T12:30:00+00:00",
        "seen": 2,
        "stats": {"fetched": 12, "not_modified": 3, "pushed": 9, "errors": 1},
    }


async def test_crawler_stats_defaults_when_redis_empty(client):
    """爬虫还没跑过（或 ``crawl:stats`` 刚被下一轮清空）时返回默认值，而不是 404/500。"""
    body = (await client.get("/api/v1/documents/crawler-stats")).json()

    assert body == {"last_run": None, "seen": 0, "stats": {}}


class _BrokenRedis:
    """读第一个键就抛连接的假客户端 —— 模拟 Redis 抖动。"""

    async def hgetall(self, *args, **kwargs):
        """:raise ConnectionError: 永远失败。"""
        raise ConnectionError("redis down")


async def test_crawler_stats_degrades_when_redis_unavailable(client):
    """Redis 不可用时接口不 500，返回空统计 —— 运维页不能被缓存打挂。"""

    async def broken_redis():
        yield _BrokenRedis()

    app.dependency_overrides[get_redis] = broken_redis
    try:
        response = await client.get("/api/v1/documents/crawler-stats")
    finally:
        app.dependency_overrides.pop(get_redis, None)

    assert response.status_code == 200
    assert response.json() == {"last_run": None, "seen": 0, "stats": {}}
