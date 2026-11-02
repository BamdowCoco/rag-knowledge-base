"""
爬虫状态（Redis）。

**为什么用 Redis 而不是 SQLite：** 项目里已经有 Redis，crawler 只需要轻量 KV；
状态丢了最多重抓一轮，接口幂等会把重复挡住，不会产生重复文档。

**客户端由外部注入：** 本模块不建连接，也不读配置 —— 测试用测试库（db 15），
生产用 compose 注入的 ``redis:6379`` ，同一份代码两边都跑。调用方必须传
``decode_responses=True`` 的 ``redis.asyncio.Redis`` 。

Key 约定（``{sha1(url)}`` 是 URL 的 sha1 十六进制串，与 02 文档一致）：

 - ``crawl:etag:{sha1(url)}``  String，上次响应的 ``ETag``
 - ``crawl:lastmod:{sha1(url)}``  String，上次响应的 ``Last-Modified``
 - ``crawl:hash:{sha1(url)}``  String，上次推送正文的 sha1（目标站无 validator 时的兜底）
 - ``crawl:seen``  Set，已推送过的 ``external_id``
 - ``crawl:id2url``  Hash，``external_id`` → 归一化 URL（连续消失时比对用）
 - ``crawl:url2id``  Hash，归一化 URL → ``external_id``（canonical 与 sitemap URL 不同时反查身份）
 - ``crawl:miss:{external_id}``  String，连续缺席轮数
 - ``crawl:last_run``  String，上次完整扫描时间（ISO）
 - ``crawl:stats``  Hash，本轮各计数
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio.client import Pipeline

# ---------- Key 前缀 / 固定 Key ----------
ETAG_PREFIX = "crawl:etag:"
LASTMOD_PREFIX = "crawl:lastmod:"
HASH_PREFIX = "crawl:hash:"
SEEN_KEY = "crawl:seen"
ID2URL_KEY = "crawl:id2url"
URL2ID_KEY = "crawl:url2id"
MISS_PREFIX = "crawl:miss:"
LAST_RUN_KEY = "crawl:last_run"
STATS_KEY = "crawl:stats"


def _sha1(text: str) -> str:
    """
    计算 URL 的 sha1 十六进制串（用于拼 Redis key，统一 UTF-8 编码）。

    :param text: 待哈希的文本（通常是归一化后的 URL）。
    :return: 40 位十六进制字符串。
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _as_text(value: Any) -> str | None:
    """
    把 Redis 返回值统一成 ``str`` （兼容未开 ``decode_responses`` 的客户端）。

    :param value: ``bytes`` 、``str`` 或 ``None`` 。
    :return: 字符串；输入为 ``None`` 时返回 ``None`` 。
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _utc_iso(dt: datetime) -> str:
    """
    把时间转成 UTC ISO 字符串。

    **朴素时间（无时区）按 UTC 解释** —— 站点解析出的 ``datetime`` 大多没有时区，
    统一贴 UTC 标签，避免同一时刻出现两种写法。

    :param dt: 待格式化的时间。
    :return: ``2006-01-02T15:04:05+00:00`` 形式的字符串。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class CrawlState:
    """
    一轮扫描的状态读写。

    **批量操作走 pipeline：** ``mark_present`` / ``forget`` 一次改三到六个 key，
    用 pipeline 发一轮往返，别逐条 await。
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        """
        :param redis: 外部的 Redis 异步客户端（ ``decode_responses=True`` ）。
        """
        self.redis = redis

    # ---------- validator / 正文哈希 ----------

    async def get_validators(self, url: str) -> tuple[str | None, str | None]:
        """
        读取某个 URL 上次保存的条件请求头。

        :param url: 归一化后的页面 URL。
        :return: ``(etag, last_modified)`` ；未保存时对应位置为 ``None`` 。
        """
        etag, last_modified = await self.redis.mget(
            f"{ETAG_PREFIX}{_sha1(url)}", f"{LASTMOD_PREFIX}{_sha1(url)}"
        )
        return _as_text(etag), _as_text(last_modified)

    async def set_validators(
        self, url: str, etag: str | None, last_modified: str | None
    ) -> None:
        """
        保存某个 URL 的条件请求头。

        **值为 ``None`` 时删除对应 key** —— 服务端不再返回 validator 时，
        留着旧值只会让下一轮带一个无意义、甚至已过期的条件头。

        :param url: 归一化后的页面 URL。
        :param etag: 响应头 ``ETag`` ，没有则 ``None`` 。
        :param last_modified: 响应头 ``Last-Modified`` ，没有则 ``None`` 。
        """
        hashed = _sha1(url)
        pipe = self.redis.pipeline()
        self._stage_set_or_delete(pipe, f"{ETAG_PREFIX}{hashed}", etag)
        self._stage_set_or_delete(pipe, f"{LASTMOD_PREFIX}{hashed}", last_modified)
        await pipe.execute()

    async def get_content_hash(self, url: str) -> str | None:
        """
        读取某个 URL 上次推送正文的哈希。

        :param url: 归一化后的页面 URL。
        :return: 哈希字符串；没有记录时返回 ``None`` 。
        """
        return _as_text(await self.redis.get(f"{HASH_PREFIX}{_sha1(url)}"))

    async def set_content_hash(self, url: str, digest: str) -> None:
        """
        保存某个 URL 本次推送正文的哈希（只有推送成功后才该调用）。

        :param url: 归一化后的页面 URL。
        :param digest: 正文哈希（通常是 sha1 十六进制串）。
        """
        await self.redis.set(f"{HASH_PREFIX}{_sha1(url)}", digest)

    # ---------- 已见身份 / URL 映射 ----------

    async def seen_ids(self) -> set[str]:
        """
        读出所有已推送过的 ``external_id`` 。

        :return: ``external_id`` 集合（复制一份，调用方可随意增删）。
        """
        ids: set[str] = set()
        for value in await self.redis.smembers(SEEN_KEY):
            text = _as_text(value)
            if text is not None:
                ids.add(text)
        return ids

    async def mark_present(self, external_id: str, url: str) -> None:
        """
        标记「本轮这个身份在场」：加入 seen、记录 URL 映射、清零缺席轮数。

        :param external_id: 文档身份（canonical URL 或其哈希）。
        :param url: 归一化后的页面 URL。
        """
        pipe = self.redis.pipeline()
        pipe.sadd(SEEN_KEY, external_id)
        pipe.hset(ID2URL_KEY, external_id, url)
        pipe.hset(URL2ID_KEY, url, external_id)
        pipe.delete(f"{MISS_PREFIX}{external_id}")
        await pipe.execute()

    async def id_url(self, external_id: str) -> str | None:
        """
        查某个身份上次记录的归一化 URL。

        :param external_id: 文档身份。
        :return: 归一化 URL；没有记录时返回 ``None`` 。
        """
        return _as_text(await self.redis.hget(ID2URL_KEY, external_id))

    async def id_by_url(self, url: str) -> str | None:
        """
        按归一化 URL 反查身份。

        **为什么需要：** ``external_id`` 优先用 canonical URL，而 304 / 404 时拿不到页面正文，
        只有请求用的 sitemap URL。两者不同（例如带不同 query）时，靠这张反向表才能删对、
        记对身份，而不是拿 sitemap URL 重新算一个不存在的 id。

        :param url: 归一化后的存储 URL。
        :return: 对应的 ``external_id`` ；没有记录时返回 ``None`` 。
        """
        return _as_text(await self.redis.hget(URL2ID_KEY, url))

    async def bump_miss(self, external_id: str) -> int:
        """
        缺席轮数 +1。

        :param external_id: 文档身份。
        :return: 累加后的缺席轮数。
        """
        return int(await self.redis.incr(f"{MISS_PREFIX}{external_id}"))

    async def forget(self, external_id: str, url: str) -> None:
        """
        彻底忘掉一个身份（删除成功后调用）：seen、URL 映射、缺席计数、validator、正文哈希。

        :param external_id: 文档身份。
        :param url: 归一化后的页面 URL（validator / 哈希按 URL 存，必须一起清）。
        """
        hashed = _sha1(url)
        # id2url 里可能记着另一个 URL（canonical 与 sitemap URL 不同），两条反向映射都清掉
        stored_url = await self.id_url(external_id)
        pipe = self.redis.pipeline()
        pipe.srem(SEEN_KEY, external_id)
        pipe.hdel(ID2URL_KEY, external_id)
        pipe.hdel(URL2ID_KEY, url)
        if stored_url and stored_url != url:
            pipe.hdel(URL2ID_KEY, stored_url)
        pipe.delete(
            f"{MISS_PREFIX}{external_id}",
            f"{ETAG_PREFIX}{hashed}",
            f"{LASTMOD_PREFIX}{hashed}",
            f"{HASH_PREFIX}{hashed}",
        )
        await pipe.execute()

    # ---------- 本轮统计 ----------

    async def start_run(self) -> None:
        """开新一轮：清空 ``crawl:stats`` （上一轮的计数不再有意义）。"""
        await self.redis.delete(STATS_KEY)

    async def add_stat(self, name: str, n: int = 1) -> None:
        """
        给本轮某个计数加 ``n`` 。

        :param name: 计数名，如 ``fetched`` / ``pushed`` / ``errors`` 。
        :param n: 增量（可为负数，但正常都是正数）。
        """
        await self.redis.hincrby(STATS_KEY, name, n)

    async def stats(self) -> dict[str, int]:
        """
        读出本轮全部计数。

        :return: ``{计数名: 数值}`` ；本轮还没写过任何计数时为空字典。
        """
        raw = await self.redis.hgetall(STATS_KEY)
        return {str(_as_text(key)): int(value) for key, value in raw.items()}

    async def set_last_run(self, dt: datetime) -> None:
        """
        记录本轮扫描完成的时间。

        :param dt: 完成时间（朴素时间按 UTC 解释）。
        """
        await self.redis.set(LAST_RUN_KEY, _utc_iso(dt))

    # ---------- 内部工具 ----------

    @staticmethod
    def _stage_set_or_delete(
        pipe: Pipeline, key: str, value: str | None
    ) -> None:
        """
        往 pipeline 里排一条「有值就 SET、没值就 DEL」。

        :param pipe: Redis pipeline。
        :param key: 目标 key。
        :param value: 要写入的值；``None`` 表示删除。
        """
        if value is None:
            pipe.delete(key)
        else:
            pipe.set(key, value)
