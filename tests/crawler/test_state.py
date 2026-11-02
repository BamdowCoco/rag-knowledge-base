"""
爬虫状态单测：直接用测试容器的 Redis（db 15，由 ``tests/conftest.py`` 的 ``redis`` fixture 提供）。

**为什么用真 Redis 而不是 fakeredis：** 项目脚手架本来就是真 Redis，状态层还用了 pipeline，
在真客户端上跑一遍才说明「一轮往返里 SET/DEL 顺序」没写错。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from crawler.state import CrawlState


def _sha1(text: str) -> str:
    """与状态层同口径的 sha1（测试里独立算一遍，别复用被测代码）。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------- validator / 正文哈希 ----------


async def test_validators_roundtrip(redis):
    """ETag / Last-Modified 写入后读回一致，key 名按 ``sha1(url)`` 。"""
    state = CrawlState(redis)
    url = "https://fastapi.tiangolo.com/zh/tutorial/"

    assert await state.get_validators(url) == (None, None)

    await state.set_validators(url, '"abc123"', "Wed, 01 Oct 2026 10:00:00 GMT")

    assert await state.get_validators(url) == ('"abc123"', "Wed, 01 Oct 2026 10:00:00 GMT")
    assert await redis.get(f"crawl:etag:{_sha1(url)}") == '"abc123"'
    assert await redis.get(f"crawl:lastmod:{_sha1(url)}") == "Wed, 01 Oct 2026 10:00:00 GMT"


async def test_set_validators_none_deletes_stale_values(redis):
    """服务端不再返回 validator 时，旧值必须删掉（否则下一轮带着过期条件头）。"""
    state = CrawlState(redis)
    url = "https://fastapi.tiangolo.com/zh/no-validator/"

    await state.set_validators(url, '"old"', "Wed, 01 Oct 2026 10:00:00 GMT")
    await state.set_validators(url, None, None)

    assert await state.get_validators(url) == (None, None)


async def test_content_hash_roundtrip(redis):
    state = CrawlState(redis)
    url = "https://fastapi.tiangolo.com/zh/hash/"

    assert await state.get_content_hash(url) is None
    await state.set_content_hash(url, "d41d8cd98f00b204e9800998ecf8427e")
    assert await state.get_content_hash(url) == "d41d8cd98f00b204e9800998ecf8427e"
    assert await redis.get(f"crawl:hash:{_sha1(url)}") == "d41d8cd98f00b204e9800998ecf8427e"


# ---------- 身份 / 缺席 / 遗忘 ----------


async def test_mark_present_and_id_url(redis):
    """ ``mark_present`` 写 seen + id2url，并清零缺席计数。"""
    state = CrawlState(redis)
    external_id = "https://fastapi.tiangolo.com/zh/mark/"
    url = "https://fastapi.tiangolo.com/zh/mark/"

    await state.mark_present(external_id, url)

    assert external_id in await state.seen_ids()
    assert await state.id_url(external_id) == url


async def test_url2id_reverse_lookup_and_forget(redis):
    """ ``url2id`` 支持按 URL 反查身份；``forget`` 后反查不到。"""
    state = CrawlState(redis)
    external_id = "https://fastapi.tiangolo.com/zh/canonical/"
    sitemap_url = "https://fastapi.tiangolo.com/zh/sitemap-entry/"

    await state.mark_present(external_id, sitemap_url)

    assert await state.id_by_url(sitemap_url) == external_id

    await state.forget(external_id, sitemap_url)

    assert await state.id_by_url(sitemap_url) is None


async def test_bump_miss_then_forget(redis):
    """连续缺席累加到上限后 ``forget`` 清掉全部状态；再 bump 从头开始。"""
    state = CrawlState(redis)
    external_id = "https://fastapi.tiangolo.com/zh/gone/"
    url = "https://fastapi.tiangolo.com/zh/gone/"

    await state.mark_present(external_id, url)
    await state.set_validators(url, '"etag"', "Wed, 01 Oct 2026 10:00:00 GMT")
    await state.set_content_hash(url, "digest")

    assert await state.bump_miss(external_id) == 1
    assert await state.bump_miss(external_id) == 2

    await state.forget(external_id, url)

    assert external_id not in await state.seen_ids()
    assert await state.id_url(external_id) is None
    assert await redis.exists(f"crawl:miss:{external_id}") == 0
    assert await redis.exists(f"crawl:etag:{_sha1(url)}") == 0
    assert await redis.exists(f"crawl:lastmod:{_sha1(url)}") == 0
    assert await redis.exists(f"crawl:hash:{_sha1(url)}") == 0
    # 缺席计数确实清空：下一次从 1 开始
    assert await state.bump_miss(external_id) == 1


async def test_mark_present_resets_miss_counter(redis):
    """页面重新出现时，旧的缺席计数要清零（否则一次抓取失败会累积成误删）。"""
    state = CrawlState(redis)
    external_id = "https://fastapi.tiangolo.com/zh/back/"
    url = "https://fastapi.tiangolo.com/zh/back/"

    await state.bump_miss(external_id)
    await state.bump_miss(external_id)
    await state.mark_present(external_id, url)

    assert await state.bump_miss(external_id) == 1


# ---------- 统计 / 上次运行 ----------


async def test_stats_accumulate_and_reset(redis):
    """ ``add_stat`` 累加；``start_run`` 清空上一轮。"""
    state = CrawlState(redis)
    await state.start_run()

    await state.add_stat("fetched")
    await state.add_stat("fetched")
    await state.add_stat("errors", 3)

    assert await state.stats() == {"fetched": 2, "errors": 3}

    await state.start_run()
    assert await state.stats() == {}


async def test_set_last_run_stores_utc_iso(redis):
    state = CrawlState(redis)
    dt = datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc)

    await state.set_last_run(dt)

    raw = await redis.get("crawl:last_run")
    assert raw is not None
    assert datetime.fromisoformat(raw) == dt
