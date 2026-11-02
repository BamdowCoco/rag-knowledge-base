"""
抓取层：``robots.txt`` 规则、按 host 限速、条件请求、重试与状态映射。

**设计取舍：**

 - 限速与重试分开：``HostRateLimiter`` 只保证“请求起始时间”的间隔与并发，
   退避的 ``sleep`` 放在限速槽之外，别让一个 429 把同 host 的并发槽全部占死
 - 重试只对 ``429`` / ``5xx`` / 网络异常生效；其余 ``4xx`` 不重试（``404`` / ``410`` 走删除）
 - 退避等待统一走模块级 ``_sleep`` ，测试里可以 monkeypatch，不必真等 1 秒
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, AsyncIterator, Literal
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    # 只用于类型标注：避免 fetch 与 config 在运行期互相 import。
    from crawler.config import CrawlConfig

logger = logging.getLogger(__name__)

FetchKind = Literal["ok", "not_modified", "gone", "skipped", "error"]

_DEFAULT_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
_GONE_STATUSES = frozenset({404, 410})


class RobotsRules:
    """
    解析后的 ``robots.txt`` 规则。

    只实现 crawler 需要的部分：

     - 按 UA token 选组；没有更具体的组时回落到 ``*``
     - ``Allow`` / ``Disallow`` 取最长匹配，等长时 ``Allow`` 优先
     - 支持 ``*`` 通配与结尾 ``$`` 锚定
     - ``Crawl-delay`` 取所选组的最大值
    """

    def __init__(
        self,
        rules: list[tuple[bool, str, re.Pattern[str]]],
        crawl_delay: float | None = None,
    ) -> None:
        """
        :param rules: ``(是否允许, 原始模式, 已编译的路径正则)`` 列表。
        :param crawl_delay: 该 UA 对应的 ``Crawl-delay`` 秒数。
        """
        self._rules = rules
        self._crawl_delay = crawl_delay

    @property
    def crawl_delay(self) -> float | None:
        """
        该 UA 对应的 ``Crawl-delay`` 。

        :return: 秒数；``robots.txt`` 未声明时为 ``None`` 。
        """
        return self._crawl_delay

    @classmethod
    def parse(cls, text: str, user_agent: str) -> "RobotsRules":
        """
        从 ``robots.txt`` 文本中挑出与 ``user_agent`` 匹配的规则组。

        同一 ``User-agent`` 出现多个组时规则合并；``Crawl-delay`` 取最大值，
        宁慢勿快。

        :param text: ``robots.txt`` 全文。
        :param user_agent: 本爬虫的 UA 字符串。
        :return: 解析后的规则对象；没有任何规则时 ``allowed`` 恒为 ``True`` 。
        """
        groups = _parse_robots_groups(text)
        ua_lower = (user_agent or "").lower()

        # 选最长、且出现在本 UA 里的 token —— 越具体越优先。
        matched_length = -1
        matched: list[_RobotsGroup] = []
        for group in groups:
            for agent in group.agents:
                if agent == "*" or agent not in ua_lower:
                    continue
                if len(agent) > matched_length:
                    matched_length = len(agent)
                    matched = [group]
                elif len(agent) == matched_length and group not in matched:
                    matched.append(group)

        if not matched:
            matched = [group for group in groups if "*" in group.agents]

        rules: list[tuple[bool, str, re.Pattern[str]]] = []
        delays: list[float] = []
        for group in matched:
            rules.extend(group.rules)
            if group.crawl_delay is not None:
                delays.append(group.crawl_delay)

        delay = max(delays) if delays else None
        return cls(rules=rules, crawl_delay=delay)

    def allowed(self, url: str) -> bool:
        """
        判断 URL 是否允许抓取。

        :param url: 待判断的 URL。
        :return: 允许时为 ``True`` ；没有匹配规则时默认允许。
        """
        if not self._rules:
            return True

        parts = urlsplit(url)
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        best_length = -1
        best_allow = True
        for allow, raw_pattern, pattern in self._rules:
            if not pattern.match(path):
                continue
            # 按 robots 原文长度比较“具体程度”，别用编译后带 ``^`` / ``$`` 的长度。
            length = len(raw_pattern)
            if length > best_length or (length == best_length and allow and not best_allow):
                best_length = length
                best_allow = allow
        return best_allow


@dataclass
class _RobotsGroup:
    """``robots.txt`` 里的一个 ``User-agent`` 组（解析中间态）。"""

    agents: list[str]
    rules: list[tuple[bool, str, re.Pattern[str]]]
    crawl_delay: float | None = None


def _robots_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """
    把 robots 路径模式编译成正则。

    :param pattern: 形如 ``/zh/*`` 、 ``/private$`` 的路径模式。
    :return: 从字符串开头匹配的正则；结尾 ``$`` 时锚定到末尾。
    """
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in core.split("*"))
    return re.compile("^" + regex + ("$" if anchored else ""))


def _parse_robots_groups(text: str) -> list[_RobotsGroup]:
    """
    按行解析 ``robots.txt`` ，返回所有规则组。

    ``User-agent`` 连续出现时属于同一组；出现规则后再遇到 ``User-agent``
    就开启新组。未知指令与空行直接忽略。

    :param text: ``robots.txt`` 全文。
    :return: 规则组列表。
    """
    groups: list[_RobotsGroup] = []
    agents: list[str] = []
    rules: list[tuple[bool, str, re.Pattern[str]]] = []
    delay: float | None = None
    has_rules = False

    def flush() -> None:
        nonlocal agents, rules, delay, has_rules
        if agents:
            groups.append(_RobotsGroup(agents=agents, rules=rules, crawl_delay=delay))
        agents = []
        rules = []
        delay = None
        has_rules = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        if key == "user-agent":
            if has_rules:
                flush()
            if value:
                agents.append(value.lower())
            continue
        if key in ("allow", "disallow"):
            has_rules = True
            if not value:
                continue
            rules.append((key == "allow", value, _robots_pattern_to_regex(value)))
            continue
        if key == "crawl-delay":
            has_rules = True
            try:
                parsed = float(value)
            except ValueError:
                continue
            if parsed >= 0:
                delay = parsed if delay is None else max(delay, parsed)

    flush()
    return groups


class HostRateLimiter:
    """
    按 host 限制并发与请求起始间隔。

    每个 host 一个 ``Semaphore`` 控制并发；另有一把调度锁记录“下一个可发起
    请求的时刻”。即使并发为 2，两个请求的起始时间也会错开 ``delay`` 秒，
    而不是同时打出去。
    """

    def __init__(self, concurrency: int, delay: float) -> None:
        """
        :param concurrency: 单 host 并发上限，必须 ``>= 1`` 。
        :param delay: 同一 host 两次请求起始时间的最小间隔（秒），必须 ``>= 0`` 。
        :raises ValueError: 并发或间隔取值非法。
        """
        if concurrency < 1:
            raise ValueError(f"concurrency 必须 >= 1，当前为 {concurrency}")
        if delay < 0:
            raise ValueError(f"delay 必须 >= 0，当前为 {delay}")
        self._concurrency = concurrency
        self._delay = delay
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._schedule_locks: dict[str, asyncio.Lock] = {}
        self._next_start: dict[str, float] = {}

    @staticmethod
    def _host_of(url: str) -> str:
        """
        取 URL 的 host（小写）；无法解析时用空串兜底。

        :param url: 目标 URL。
        :return: host 字符串。
        """
        try:
            return (urlsplit(url).hostname or "").lower()
        except ValueError:
            return ""

    def _semaphore_for(self, host: str) -> asyncio.Semaphore:
        """
        取（必要时创建）该 host 的并发信号量。

        :param host: host 字符串。
        :return: 信号量。
        """
        semaphore = self._semaphores.get(host)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._concurrency)
            self._semaphores[host] = semaphore
        return semaphore

    async def _wait_turn(self, host: str) -> None:
        """
        在调度锁内预占一个起始时刻，锁外等待到该时刻。

        :param host: host 字符串。
        """
        if self._delay <= 0:
            return

        lock = self._schedule_locks.get(host)
        if lock is None:
            lock = asyncio.Lock()
            self._schedule_locks[host] = lock

        async with lock:
            now = time.monotonic()
            start = max(now, self._next_start.get(host, 0.0))
            self._next_start[host] = start + self._delay

        wait = start - time.monotonic()
        if wait > 0:
            await _sleep(wait)

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        """
        占用一个请求槽：先拿并发信号量，再等到本 host 允许的起始时刻。

        :param url: 本次要请求的 URL。
        """
        host = self._host_of(url)
        semaphore = self._semaphore_for(host)
        await semaphore.acquire()
        try:
            await self._wait_turn(host)
            yield
        finally:
            semaphore.release()


@dataclass
class FetchResult:
    """
    单次页面抓取的结果。

    :param kind: ``ok`` / ``not_modified`` / ``gone`` / ``skipped`` / ``error`` 。
    :param url: 请求的 URL。
    :param text: ``ok`` 时的正文 HTML；其他状态为 ``None`` 。
    :param status_code: HTTP 状态码；网络异常时为 ``None`` 。
    :param etag: 响应里的 ``ETag`` （或 304 时沿用的缓存值）。
    :param last_modified: 响应里的 ``Last-Modified`` （或 304 时沿用的缓存值）。
    :param reason: 跳过 / 失败原因，便于日志排查。
    """

    kind: FetchKind
    url: str
    text: str | None = None
    status_code: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    reason: str | None = None


async def _sleep(seconds: float) -> None:
    """
    可替换的等待入口：测试 monkeypatch 这里，避免真等退避时间。

    :param seconds: 等待秒数。
    """
    await asyncio.sleep(seconds)


def _parse_retry_after(value: str | None) -> float | None:
    """
    解析 ``Retry-After`` ：既支持秒数，也支持 HTTP-date。

    :param value: 响应头原文。
    :return: 需要等待的秒数（不小于 0）；缺失或无法解析时为 ``None`` 。
    """
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """
    计算第 ``attempt`` 次重试前的等待时间。

    ``429`` 优先用 ``Retry-After`` ；否则指数退避 ``1s → 2s → 4s`` ，
    再叠加不超过底数一半的抖动，避免多个请求在同一时刻重新撞上去。

    :param attempt: 已失败次数，从 0 开始。
    :param retry_after: ``Retry-After`` 响应头。
    :return: 等待秒数。
    """
    if retry_after is not None:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            return parsed
    base = float(2**attempt)
    return base + random.uniform(0.0, base * 0.5)


def _request_headers(
    cfg: "CrawlConfig",
    *,
    etag: str | None,
    last_modified: str | None,
    replay: bool,
) -> dict[str, str]:
    """
    构造页面请求头。

    :param cfg: 爬虫配置。
    :param etag: 上次响应的 ``ETag`` 。
    :param last_modified: 上次响应的 ``Last-Modified`` 。
    :param replay: 重放模式：跳过条件请求。
    :return: 请求头字典。
    """
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": _DEFAULT_ACCEPT,
    }
    if not replay:
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
    return headers


async def _get(
    client: httpx.AsyncClient,
    url: str,
    *,
    cfg: "CrawlConfig",
    limiter: HostRateLimiter,
    headers: dict[str, str],
) -> httpx.Response:
    """
    带限速与重试的 GET。

    只对 ``429`` / ``5xx`` / 网络异常重试，最多 ``cfg.max_retries`` 次；
    其余状态码原样返回，由调用方做业务映射。

    :param client: 复用的 ``httpx.AsyncClient`` 。
    :param url: 目标 URL。
    :param cfg: 爬虫配置。
    :param limiter: 限速器。
    :param headers: 请求头。
    :return: 最终响应（可能是最后一次重试仍失败的 429/5xx）。
    :raises httpx.HTTPError: 网络异常且重试次数用尽。
    """
    retries = max(0, int(cfg.max_retries))

    for attempt in range(retries + 1):
        try:
            async with limiter.slot(url):
                response = await client.get(
                    url,
                    headers=headers,
                    timeout=cfg.timeout_seconds,
                    follow_redirects=True,
                )
        except httpx.HTTPError as exc:
            if attempt >= retries:
                raise
            delay = _retry_delay(attempt)
            logger.warning(
                "请求 %s 网络异常（第 %d/%d 次重试）：%s；%.2fs 后重试",
                url,
                attempt + 1,
                retries,
                exc,
                delay,
            )
            await _sleep(delay)
            continue

        status = response.status_code
        if status == 429 or 500 <= status < 600:
            if attempt >= retries:
                return response
            retry_after = response.headers.get("Retry-After") if status == 429 else None
            delay = _retry_delay(attempt, retry_after)
            logger.warning(
                "请求 %s 返回 HTTP %d（第 %d/%d 次重试）；%.2fs 后重试",
                url,
                status,
                attempt + 1,
                retries,
                delay,
            )
            await _sleep(delay)
            continue

        return response

    # 循环要么返回、要么抛出；这里只是让类型检查器安心。
    raise RuntimeError(f"请求 {url} 的重试流程意外结束")


def _declared_too_large(response: httpx.Response, max_bytes: int) -> int | None:
    """
    检查 ``Content-Length`` 是否超限。

    :param response: HTTP 响应。
    :param max_bytes: 允许的最大字节数。
    :return: 超限时返回声明的字节数；未声明或未超限返回 ``None`` 。
    """
    declared = response.headers.get("Content-Length")
    if not declared:
        return None
    try:
        size = int(declared)
    except ValueError:
        return None
    return size if size > max_bytes else None


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    cfg: "CrawlConfig",
    limiter: HostRateLimiter,
    etag: str | None = None,
    last_modified: str | None = None,
    replay: bool = False,
) -> FetchResult:
    """
    条件 GET 一个页面并映射成 ``FetchResult`` 。

    状态映射：

     - ``200`` → ``ok`` （先看 ``Content-Length`` ，读完再按实际字节数兜底）
     - ``304`` → ``not_modified``
     - ``404`` / ``410`` → ``gone`` （调用方发删除信号）
     - 其余 ``4xx`` → ``error`` ，不重试
     - ``429`` / ``5xx`` / 网络异常 → 重试后仍失败则 ``error``
     - 超过 ``cfg.max_page_bytes`` → ``skipped``

    :param client: 复用的 ``httpx.AsyncClient`` 。
    :param url: 目标页面。
    :param cfg: 爬虫配置。
    :param limiter: 限速器。
    :param etag: 上次响应的 ``ETag`` ；``replay`` 时不发送。
    :param last_modified: 上次响应的 ``Last-Modified`` ；``replay`` 时不发送。
    :param replay: 重放模式：跳过条件请求。
    :return: 抓取结果。
    """
    headers = _request_headers(cfg, etag=etag, last_modified=last_modified, replay=replay)
    try:
        response = await _get(client, url, cfg=cfg, limiter=limiter, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("抓取 %s 失败：%s", url, exc)
        return FetchResult(kind="error", url=url, reason=f"网络异常：{exc.__class__.__name__}: {exc}")

    status = response.status_code
    response_etag = response.headers.get("ETag")
    response_last_modified = response.headers.get("Last-Modified")

    if status == 304:
        return FetchResult(
            kind="not_modified",
            url=url,
            status_code=status,
            etag=response_etag or etag,
            last_modified=response_last_modified or last_modified,
        )

    if status in _GONE_STATUSES:
        return FetchResult(
            kind="gone",
            url=url,
            status_code=status,
            reason=f"HTTP {status}",
        )

    if status == 200:
        declared = _declared_too_large(response, cfg.max_page_bytes)
        if declared is not None:
            logger.warning("跳过 %s：Content-Length %d 超过上限 %d", url, declared, cfg.max_page_bytes)
            return FetchResult(
                kind="skipped",
                url=url,
                status_code=status,
                etag=response_etag,
                last_modified=response_last_modified,
                reason=f"Content-Length {declared} 超过上限 {cfg.max_page_bytes}",
            )
        if len(response.content) > cfg.max_page_bytes:
            logger.warning(
                "跳过 %s：响应 %d 字节超过上限 %d",
                url,
                len(response.content),
                cfg.max_page_bytes,
            )
            return FetchResult(
                kind="skipped",
                url=url,
                status_code=status,
                etag=response_etag,
                last_modified=response_last_modified,
                reason=f"响应 {len(response.content)} 字节超过上限 {cfg.max_page_bytes}",
            )
        return FetchResult(
            kind="ok",
            url=url,
            text=response.text,
            status_code=status,
            etag=response_etag,
            last_modified=response_last_modified,
        )

    return FetchResult(
        kind="error",
        url=url,
        status_code=status,
        reason=f"HTTP {status}",
    )


async def fetch_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    cfg: "CrawlConfig",
    limiter: HostRateLimiter,
    max_bytes: int,
) -> bytes:
    """
    抓取非页面资源（``robots.txt`` / sitemap）的原始字节。

    与 ``fetch_page`` 共用限速与重试规则；任何非 ``200`` 或超限都抛
    ``RuntimeError`` ，由调度层把本轮标记为失败，保留旧状态。

    :param client: 复用的 ``httpx.AsyncClient`` 。
    :param url: 资源地址。
    :param cfg: 爬虫配置。
    :param limiter: 限速器。
    :param max_bytes: 允许的最大字节数。
    :return: 响应原始字节。
    :raises RuntimeError: 网络异常、非 200 或超过大小上限。
    """
    headers = {"User-Agent": cfg.user_agent, "Accept": "*/*"}
    try:
        response = await _get(client, url, cfg=cfg, limiter=limiter, headers=headers)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"GET {url} 失败：{exc}") from exc

    status = response.status_code
    if status != 200:
        raise RuntimeError(f"GET {url} 返回 HTTP {status}")

    declared = _declared_too_large(response, max_bytes)
    if declared is not None:
        raise RuntimeError(f"GET {url} 的 Content-Length {declared} 超过上限 {max_bytes}")

    content = response.content
    if len(content) > max_bytes:
        raise RuntimeError(f"GET {url} 的响应 {len(content)} 字节超过上限 {max_bytes}")
    return content
