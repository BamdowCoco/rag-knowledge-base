"""分布式锁：抢锁互斥 + **Lua 比对 token** 释放（错 token 不能删掉别人的锁）。"""
import asyncio

import pytest

from app.services.lock_service import (
    LockNotAcquired,
    acquire_lock,
    document_lock,
    lock_key,
    release_lock,
)

TTL_MS = 5000


async def test_second_acquire_fails(redis):
    key = lock_key("hash-a")
    assert await acquire_lock(redis, key, TTL_MS) is not None
    assert await acquire_lock(redis, key, TTL_MS) is None       # 已被占，抢不到


async def test_release_with_wrong_token_keeps_lock(redis):
    """
    这就是「裸 DEL 会删掉别人的锁」的那条不变式：
    拿着 **过期的旧 token** 来释放，必须删不掉，且锁仍在。
    """
    key = lock_key("hash-b")
    token = await acquire_lock(redis, key, TTL_MS)

    assert await release_lock(redis, key, "somebody-elses-token") is False
    assert await redis.get(key) == token                        # 锁还在

    assert await release_lock(redis, key, token) is True        # 自己的 token 才删得掉
    assert await redis.get(key) is None


async def test_document_lock_raises_when_taken(redis):
    async with document_lock(redis, "hash-c"):
        with pytest.raises(LockNotAcquired):
            async with document_lock(redis, "hash-c"):
                pass


async def test_document_lock_releases_on_exception(redis):
    """临界区抛异常也要释放（try/finally）—— 否则这篇文档就永远锁死了。"""
    with pytest.raises(RuntimeError):
        async with document_lock(redis, "hash-d"):
            raise RuntimeError("临界区炸了")

    async with document_lock(redis, "hash-d"):      # 能再抢到 = 确实释放了
        pass


async def test_lock_expires_by_ttl(redis):
    """TTL 是「崩溃安全」的代价：持有者挂了，锁最终会自己过期。"""
    key = lock_key("hash-e")
    assert await acquire_lock(redis, key, 50) is not None
    await asyncio.sleep(0.15)
    assert await redis.get(key) is None
    assert await acquire_lock(redis, key, TTL_MS) is not None
