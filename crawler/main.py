"""
调度层：一轮扫描（robots → sitemap → 抓取 / 抽取 → 推送 → 连续消失判定）与常驻循环。

**并发结构：** 页面处理全部交给 ``asyncio.gather`` ，真正的并发上限与请求起始间隔由
``HostRateLimiter`` 控制（单 host 信号量 + 起始时刻调度）。推送不与抓取并发：本轮
待推送的 payload 先收集起来，再按 ``BULK_SIZE`` 分组调用批量接口。这样既用上了
``/documents/bulk`` ，又能在某一批失败时明确知道「这一批都没推成」——整批状态不写，
下一轮重推，接口幂等会把重复挡住。

**身份与存储 URL：** ``external_id`` 优先用页面声明的 canonical URL（同 host 才接受），
否则退回本轮的归一化 URL；正文哈希与条件请求头都按这个存储 URL 记。

**连续消失：** sitemap 里没有、且本轮也没处理成功的身份才累计缺席；缺席达到
``CRAWL_MISS_LIMIT`` 才发删除信号。URL 还在 sitemap 里（只是本轮抓取失败）不计数，
避免一次网络抖动误删。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
import redis.asyncio as aioredis

from crawler.config import CrawlConfig
from crawler.extract import extract_page
from crawler.fetch import (
    HostRateLimiter,
    RobotsRules,
    FetchResult,
    fetch_bytes,
    fetch_page,
)
from crawler.push import build_payload, push_documents, send_delete
from crawler.sitemap import (
    SitemapUrl,
    filter_urls,
    make_external_id,
    normalize_url,
    parse_sitemap,
)
from crawler.state import CrawlState

logger = logging.getLogger(__name__)

# robots.txt 很小；给 512KB 已经非常宽松，超过多半不是正常文件
_ROBOTS_MAX_BYTES = 512 * 1024
# sitemap index 最多再往下递归两层（主 sitemap → 子 sitemap → 孙 sitemap）
_MAX_SITEMAP_DEPTH = 2
# 接入接口的单批上限（与 app/schemas/document.py 的 BulkDocumentCreate 一致）
_MAX_BULK_DOCUMENTS = 200

# 汇总日志的固定顺序：看日志时每轮字段位置一致，便于横向比较
_STAT_ORDER = (
    "fetched",
    "not_modified",
    "pushed",
    "created",
    "updated",
    "duplicates",
    "rejected",
    "deleted",
    "skipped",
    "missing",
    "errors",
)


@dataclass
class _PendingPush:
    """
    一篇已抽取、等待批量推送的文档。

    :param external_id: 稳定身份（canonical URL 或其哈希）。
    :param storage_url: 归一化后的存储 URL（正文哈希 / validator 的 key 口径）。
    :param payload: ``/documents/bulk`` 的单篇请求体。
    :param etag: 本次响应的 ``ETag`` 。
    :param last_modified: 本次响应的 ``Last-Modified`` 。
    :param digest: 正文归一化空白后的 sha256。
    """

    external_id: str
    storage_url: str
    payload: dict[str, Any]
    etag: str | None
    last_modified: str | None
    digest: str


@dataclass(frozen=True)
class _UrlOutcome:
    """
    单 URL 处理的返回值。

    :param external_id: 本轮确认在场的身份。
    :param pending: 需要推送的文档；``None`` 表示已处理完（304 / 正文未变 / 删除等）。
    """

    external_id: str
    pending: _PendingPush | None = None


async def run_once(
    cfg: CrawlConfig, client: httpx.AsyncClient, state: CrawlState
) -> dict[str, int]:
    """
    跑一轮完整扫描。

    **早期失败不写 ``crawl:last_run`` ：** robots 或 sitemap 拉不到时这一轮不算完成，
    保留上次成功时间，下一轮再试。

    :param cfg: 爬虫配置。
    :param client: 复用的 HTTP 客户端。
    :param state: Redis 状态。
    :return: 本轮统计（Redis 里的 ``crawl:stats`` 快照）。
    """
    await state.start_run()

    robots, limiter = await _prepare_robots(cfg, client)
    if robots is None or limiter is None:
        await state.add_stat("errors")
        return await state.stats()

    try:
        entries = await _collect_sitemap_entries(cfg, client, limiter)
    except Exception as exc:  # noqa: BLE001 - sitemap 任何失败都不该拖垮常驻循环
        logger.error("拉取 sitemap 失败，本轮不抓：%s", exc)
        await state.add_stat("errors")
        return await state.stats()

    urls = filter_urls(
        entries,
        prefix=cfg.url_prefix,
        max_pages=cfg.max_pages,
        allowed=robots.allowed,
    )
    logger.info("sitemap 解析出 %d 条，过滤后本轮抓取 %d 条", len(entries), len(urls))

    if not urls:
        # 过滤后一条不剩，多半是前缀 / sitemap 配错。此时若照常做连续消失判定，
        # 会把上一轮抓到的页面全部记成缺席，两轮后误删 —— 宁可本轮不判。
        logger.error("过滤后没有可抓 URL（前缀或 sitemap 可能配错），本轮跳过连续消失判定")
        await state.add_stat("errors")
        return await state.stats()

    outcomes = await asyncio.gather(
        *[
            _process_url(url, cfg=cfg, client=client, state=state, limiter=limiter)
            for url in urls
        ]
    )

    present: set[str] = set()
    pending: list[_PendingPush] = []
    for outcome in outcomes:
        if outcome is None:
            continue
        if outcome.pending is None:
            present.add(outcome.external_id)
        else:
            pending.append(outcome.pending)

    await _push_pending(cfg, client, state, pending, present)

    sweep = await _sweep_missing(state, set(urls), present, cfg, client)
    for name, value in sweep.items():
        if value:
            await state.add_stat(name, value)

    await state.set_last_run(datetime.now(timezone.utc))
    return await state.stats()


async def _prepare_robots(
    cfg: CrawlConfig, client: httpx.AsyncClient
) -> tuple[RobotsRules | None, HostRateLimiter | None]:
    """
    拉取并解析 ``robots.txt`` ，返回规则与页面抓取用的限速器。

    **两步限速器：** 拉 robots 时还不知道它声明的 ``Crawl-delay`` ，先用配置的间隔；
    拿到规则后再按 ``max(配置间隔, Crawl-delay)`` 重建，用于 sitemap 与页面请求。

    :param cfg: 爬虫配置。
    :param client: HTTP 客户端。
    :return: ``(规则, 限速器)`` ；任何一步失败返回 ``(None, None)`` 。
    """
    parts = urlsplit(cfg.url_prefix or cfg.sitemap_url)
    if not parts.scheme or not parts.netloc:
        logger.error("无法从 CRAWL_URL_PREFIX 解析出站点根：%r", cfg.url_prefix)
        return None, None

    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    boot_limiter = HostRateLimiter(concurrency=cfg.concurrency, delay=cfg.delay_seconds)
    try:
        raw = await fetch_bytes(
            client,
            robots_url,
            cfg=cfg,
            limiter=boot_limiter,
            max_bytes=_ROBOTS_MAX_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 - 拉不到 robots 就整轮不抓
        logger.error("拉取 %s 失败，本轮不抓：%s", robots_url, exc)
        return None, None

    rules = RobotsRules.parse(raw.decode("utf-8", errors="replace"), cfg.user_agent)
    if not rules.allowed(cfg.url_prefix):
        logger.error("robots.txt 禁止抓取前缀 %s，本轮不抓", cfg.url_prefix)
        return None, None

    delay = max(cfg.delay_seconds, rules.crawl_delay or 0.0)
    limiter = HostRateLimiter(concurrency=cfg.concurrency, delay=delay)
    logger.info("robots.txt 允许抓取，有效请求间隔 %.2fs", delay)
    return rules, limiter


async def _collect_sitemap_entries(
    cfg: CrawlConfig, client: httpx.AsyncClient, limiter: HostRateLimiter
) -> list[SitemapUrl]:
    """
    拉取 sitemap；遇到 sitemap index 时递归子 sitemap（最多 ``_MAX_SITEMAP_DEPTH`` 层）。

    **子 sitemap 失败不拖垮整轮：** 主 sitemap 失败向上抛（本轮不抓）；某个子 sitemap
    失败只记警告并跳过，能抓到多少算多少。``.gz`` 子 sitemap 不在本轮范围内，跳过。

    :param cfg: 爬虫配置。
    :param client: HTTP 客户端。
    :param limiter: 限速器。
    :return: 所有页面条目（未规范化、未过滤）。
    :raises Exception: 主 sitemap 拉取 / 解析失败。
    """
    entries: list[SitemapUrl] = []
    visited: set[str] = set()

    async def load(url: str, depth: int) -> None:
        """
        递归加载一个 sitemap 文件。

        :param url: sitemap 地址。
        :param depth: 当前层级（主 sitemap 为 0）。
        """
        normalized = normalize_url(url) or url
        if normalized in visited:
            return
        visited.add(normalized)

        if normalized.lower().endswith(".gz"):
            logger.warning("跳过 .gz sitemap（本轮不支持解压）：%s", url)
            return

        try:
            raw = await fetch_bytes(
                client,
                normalized,
                cfg=cfg,
                limiter=limiter,
                max_bytes=cfg.max_sitemap_bytes,
            )
            document = parse_sitemap(raw)
        except Exception as exc:  # noqa: BLE001 - 子 sitemap 失败只跳过
            if depth == 0:
                raise
            logger.warning("子 sitemap 拉取 / 解析失败，跳过 %s：%s", url, exc)
            return

        if document.sitemaps:
            if depth >= _MAX_SITEMAP_DEPTH:
                logger.warning(
                    "sitemap index 超过 %d 层，忽略更深一层：%s", _MAX_SITEMAP_DEPTH, url
                )
                return
            for child in document.sitemaps:
                await load(child, depth + 1)
            return

        entries.extend(document.urls)
        logger.info("sitemap %s 解析出 %d 个页面", normalized, len(document.urls))

    await load(cfg.sitemap_url, 0)
    return entries


async def _process_url(
    url: str,
    *,
    cfg: CrawlConfig,
    client: httpx.AsyncClient,
    state: CrawlState,
    limiter: HostRateLimiter,
) -> _UrlOutcome | None:
    """
    抓取并处理一个页面。

    状态映射：

     - ``gone`` → 发删除信号；接口确认（``True`` / ``False`` 皆可，幂等）才 ``forget`` ，
       请求失败（``None``）保留状态下一轮再试
     - ``error`` → ``errors++`` ，不删
     - ``not_modified`` → ``not_modified++`` 、``mark_present``
     - ``ok`` → 抽取；正文哈希没变则按未变更处理；否则返回待推送记录

    :param url: 本轮要抓的归一化 URL。
    :param cfg: 爬虫配置。
    :param client: HTTP 客户端。
    :param state: Redis 状态。
    :param limiter: 限速器。
    :return: 处理结果；``None`` 表示无需记入 present（删除 / 错误 / 跳过）。
    """
    norm_url = normalize_url(url) or url

    try:
        if cfg.replay:
            etag, last_modified = None, None
        else:
            etag, last_modified = await state.get_validators(norm_url)

        fetched = await fetch_page(
            client,
            norm_url,
            cfg=cfg,
            limiter=limiter,
            etag=etag,
            last_modified=last_modified,
            replay=cfg.replay,
        )

        if fetched.kind == "gone":
            await _handle_gone(cfg, client, state, norm_url, fetched)
            return None

        if fetched.kind == "error":
            await state.add_stat("errors")
            logger.warning("抓取失败：%s（%s）", norm_url, fetched.reason)
            return None

        if fetched.kind == "not_modified":
            await state.add_stat("not_modified")
            # 304 没有正文，拿不到 canonical；先按 URL 反查上次推送的身份
            external_id = await state.id_by_url(norm_url) or make_external_id(norm_url)
            await state.mark_present(external_id, norm_url)
            return _UrlOutcome(external_id)

        if fetched.kind == "skipped":
            # 页面太大：本轮不用它，但要记 validator，否则每轮都白下载一遍
            await state.add_stat("skipped")
            await _update_validators_if_any(state, norm_url, fetched)
            logger.warning("跳过页面：%s（%s）", norm_url, fetched.reason)
            return None

        # kind == "ok"
        await state.add_stat("fetched")
        page = extract_page(
            fetched.text or "",
            url=norm_url,
            min_chars=cfg.min_content_chars,
        )
        if page is None:
            await state.add_stat("skipped")
            await _update_validators_if_any(state, norm_url, fetched)
            logger.warning("正文为空或过短，跳过：%s", norm_url)
            return None

        storage_url = _storage_url(norm_url, page.canonical_url)
        external_id = make_external_id(storage_url)
        digest = _content_digest(page.content)

        if not cfg.replay and await state.get_content_hash(storage_url) == digest:
            # 目标站不返回 validator，正文哈希就是“没变”的兜底判据
            await state.add_stat("not_modified")
            await state.mark_present(external_id, storage_url)
            await state.set_validators(storage_url, fetched.etag, fetched.last_modified)
            return _UrlOutcome(external_id)

        payload = build_payload(url=storage_url, external_id=external_id, page=page)
        if payload is None:
            # URL 超过接口的 500 字符上限：这篇永远推不进去，记 validator 后不再白抓
            await state.add_stat("skipped")
            await _update_validators_if_any(state, storage_url, fetched)
            return None

        return _UrlOutcome(
            external_id,
            pending=_PendingPush(
                external_id=external_id,
                storage_url=storage_url,
                payload=payload,
                etag=fetched.etag,
                last_modified=fetched.last_modified,
                digest=digest,
            ),
        )
    except Exception:  # noqa: BLE001 - 单页异常不能中断整轮
        logger.exception("处理页面时出现未预期异常：%s", norm_url)
        await state.add_stat("errors")
        return None


async def _handle_gone(
    cfg: CrawlConfig,
    client: httpx.AsyncClient,
    state: CrawlState,
    norm_url: str,
    fetched: FetchResult,
) -> None:
    """
    处理 404 / 410：发删除信号，接口确认后忘掉本地状态。

    :param cfg: 爬虫配置。
    :param client: HTTP 客户端。
    :param state: Redis 状态。
    :param norm_url: 归一化后的页面 URL。
    :param fetched: 抓取结果（ ``kind == "gone"`` ）。
    """
    external_id = await state.id_by_url(norm_url) or make_external_id(norm_url)
    deleted = await send_delete(client, cfg=cfg, state=state, external_id=external_id)
    if deleted is None:
        await state.add_stat("errors")
        logger.warning("删除信号发送失败，保留状态下一轮再试：%s", external_id)
        return

    await state.forget(external_id, norm_url)
    await state.add_stat("deleted")
    logger.info("页面已消失（%s），删除信号已确认：%s", fetched.reason, external_id)


async def _update_validators_if_any(
    state: CrawlState, url: str, fetched: FetchResult
) -> None:
    """
    响应里带 validator 时更新缓存（没有就不写，避免无意义往返）。

    :param state: Redis 状态。
    :param url: 存储 URL。
    :param fetched: 抓取结果。
    """
    if fetched.etag is not None or fetched.last_modified is not None:
        await state.set_validators(url, fetched.etag, fetched.last_modified)


async def _push_pending(
    cfg: CrawlConfig,
    client: httpx.AsyncClient,
    state: CrawlState,
    pending: list[_PendingPush],
    present: set[str],
) -> None:
    """
    按 ``BULK_SIZE`` 分组推送待推文档；成功一组才写状态。

    **为什么按“组”判成功：** ``push_documents`` 的返回值是整批汇总，只知道这一批里
    有 ``failed`` 篇没送达，不知道是哪几篇。整批不写状态、下一轮重推最安全
    （去重接口会挡住已成功的那些）。

    :param cfg: 爬虫配置。
    :param client: HTTP 客户端。
    :param state: Redis 状态。
    :param pending: 待推送记录。
    :param present: 本轮确认在场的身份集合（成功推送后追加）。
    """
    batch_size = max(1, min(int(cfg.bulk_size), _MAX_BULK_DOCUMENTS))

    for start in range(0, len(pending), batch_size):
        group = pending[start : start + batch_size]
        try:
            result = await push_documents(
                client,
                [item.payload for item in group],
                cfg=cfg,
                state=state,
            )
        except Exception:  # noqa: BLE001 - 单批异常不能中断整轮
            logger.exception("推送批次异常，%d 篇留待下一轮", len(group))
            await state.add_stat("errors", len(group))
            continue

        await state.add_stat(
            "pushed", result["created"] + result["updated"] + result["duplicates"]
        )
        for name in ("created", "updated", "duplicates", "rejected"):
            if result[name]:
                await state.add_stat(name, result[name])

        if result["failed"]:
            await state.add_stat("errors", result["failed"])
            logger.warning("一批 %d 篇推送失败，状态不写，下一轮重推", result["failed"])
            continue

        for item in group:
            await state.set_content_hash(item.storage_url, item.digest)
            # 值为 None 也写：会把旧 validator 删掉，避免下一轮带过期条件头
            await state.set_validators(item.storage_url, item.etag, item.last_modified)
            await state.mark_present(item.external_id, item.storage_url)

        present.update(item.external_id for item in group)


async def _sweep_missing(
    state: CrawlState,
    sitemap_urls: set[str],
    present_ids: set[str],
    cfg: CrawlConfig,
    client: httpx.AsyncClient,
) -> dict[str, int]:
    """
    连续消失判定：sitemap 里没有、本轮也没处理成功的身份才累计缺席。

    **为什么还要看 URL 在不在 sitemap：** 页面可能只是这一轮抓取失败（超时 / 5xx），
    URL 仍在 sitemap 里 —— 这时不能算缺席，否则一次网络抖动就会走向误删。

    :param state: Redis 状态。
    :param sitemap_urls: 本轮 sitemap 归一化 URL 集合。
    :param present_ids: 本轮处理成功的身份集合。
    :param cfg: 爬虫配置。
    :param client: HTTP 客户端（发删除信号）。
    :return: ``{"deleted": n, "missing": n, "errors": n}`` 。
    """
    counts = {"deleted": 0, "missing": 0, "errors": 0}
    miss_limit = max(1, int(cfg.miss_limit))

    seen = await state.seen_ids()
    for external_id in seen - present_ids:
        url = await state.id_url(external_id)
        if url is not None and normalize_url(url) in sitemap_urls:
            # 还在 sitemap 里，只是本轮没抓成 —— 不计缺席
            continue

        misses = await state.bump_miss(external_id)
        if misses < miss_limit:
            counts["missing"] += 1
            logger.info("身份暂时缺席（%d/%d）：%s", misses, miss_limit, external_id)
            continue

        deleted = await send_delete(client, cfg=cfg, state=state, external_id=external_id)
        if deleted is None:
            # 接口没确认就不能 forget：否则这篇文档再也不会被重试删除
            counts["errors"] += 1
            logger.warning("连续消失但删除信号失败，保留状态：%s", external_id)
            continue

        await state.forget(external_id, url or "")
        counts["deleted"] += 1
        logger.info("连续 %d 轮缺席，已确认删除：%s", misses, external_id)

    return counts


def _storage_url(page_url: str, canonical_url: str | None) -> str:
    """
    选存储 URL：canonical 优先，但只接受与目标同 host 的绝对地址。

    :param page_url: 本轮请求的归一化 URL。
    :param canonical_url: 抽取出的 canonical URL。
    :return: 归一化后的存储 URL；canonical 不可用时退回 ``page_url`` 。
    """
    if not canonical_url:
        return page_url

    normalized = normalize_url(canonical_url)
    if not normalized:
        return page_url
    if _host_of(normalized) != _host_of(page_url):
        logger.warning("忽略跨 host 的 canonical：%s（页面为 %s）", canonical_url, page_url)
        return page_url
    return normalized


def _host_of(url: str) -> str:
    """
    取 URL 的 host（小写）。

    :param url: URL。
    :return: host；无法解析时为空串。
    """
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _content_digest(content: str) -> str:
    """
    计算正文哈希：先压掉所有空白差异，再取 sha256。

    **为什么要归一化空白：** 同一篇文档在不同轮次里换行、缩进可能有细微差别，
    但检索结果没变；不压空白会让哈希每轮都变，白白重推。

    :param content: 抽取后的正文。
    :return: 64 位十六进制 sha256。
    """
    normalized = " ".join(content.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _format_stats(stats: dict[str, int]) -> str:
    """
    把统计字典格式化成一行固定顺序的日志。

    :param stats: ``crawl:stats`` 快照。
    :return: ``fetched=1 not_modified=2 ...`` 形式的字符串。
    """
    return " ".join(f"{name}={stats.get(name, 0)}" for name in _STAT_ORDER)


async def main() -> None:
    """
    进程入口：构造配置 / 客户端 / Redis，然后按 ``CRAWL_ONCE`` 跑一轮或常驻轮询。

    :return: 无返回值；``CRAWL_ONCE=true`` 时跑完一轮即退出。
    """
    cfg = CrawlConfig.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [crawler] %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx 默认每个请求打一条 INFO；爬虫一轮几十页，会把真正的汇总信息淹掉
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger.info(
        "crawler 启动：sitemap=%s prefix=%s max_pages=%d once=%s replay=%s",
        cfg.sitemap_url,
        cfg.url_prefix,
        cfg.max_pages,
        cfg.once,
        cfg.replay,
    )

    redis = aioredis.Redis(
        host=cfg.redis_host,
        port=cfg.redis_port,
        db=cfg.redis_db,
        decode_responses=True,
    )
    try:
        state = CrawlState(redis)
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=cfg.timeout_seconds
        ) as client:
            while True:
                stats = await run_once(cfg, client, state)
                logger.info("本轮结束：%s", _format_stats(stats))
                if cfg.once:
                    return
                logger.info("等待 %d 秒后开始下一轮", cfg.interval_seconds)
                await asyncio.sleep(cfg.interval_seconds)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
