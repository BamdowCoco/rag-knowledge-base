"""
推送层：批量写入 ``POST /api/v1/documents/bulk`` 与删除信号 ``POST /api/v1/documents/delete`` 。

**重试口径：** 只重试「网络异常 / 5xx / 429」；4xx 是请求本身的问题（例如正文被拒），
重试只会重复犯错，直接返回让调用方记错误。``cfg.max_retries`` 表示初始尝试之外的
**额外** 重试次数，所以最多尝试 ``max_retries + 1`` 次。

**测试友好：** 等待走模块级 ``_sleep``，单测 monkeypatch 掉它就不用真等。
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

import httpx

from crawler.config import CrawlConfig
from crawler.state import CrawlState

if TYPE_CHECKING:
    # 只为类型标注引入；运行时不依赖 crawler.extract，避免两个模块互相拖住导入顺序
    from crawler.extract import ExtractedPage

logger = logging.getLogger(__name__)

# 与现有接入接口的约定（见 ``app/schemas/document.py`` ）
SOURCE = "web"
MAX_TITLE_CHARS = 500
MAX_AUTHOR_CHARS = 120
MAX_URL_CHARS = 500
MAX_BULK_DOCUMENTS = 200

BULK_PATH = "/api/v1/documents/bulk"
DELETE_PATH = "/api/v1/documents/delete"

# ``BulkResult`` 里要累计的字段
_RESULT_KEYS = ("received", "created", "updated", "duplicates", "rejected")

# 退避上限：再长就不如让本轮失败、下一轮再试
_MAX_BACKOFF_SECONDS = 30.0


async def _sleep(seconds: float) -> None:
    """
    可替换的等待函数。

    :param seconds: 等待秒数。
    """
    await asyncio.sleep(seconds)


def _iso_or_none(value: datetime | None) -> str | None:
    """
    把可选时间转成 UTC ISO 字符串。

    :param value: 时间；``None`` 表示源页面没有时间信息。
    :return: ISO 字符串或 ``None`` 。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def build_payload(*, url: str, external_id: str, page: ExtractedPage) -> dict[str, Any] | None:
    """
    把抽取结果拼成 ``/documents/bulk`` 的单篇请求体。

    **URL 超长返回 ``None`` 而不是抛异常：** 五百字符上限来自接口校验，真实文档站的 URL
    偶尔会超（带大量 query）；这属于「这篇跳过」而不是「整轮失败」，返回 ``None``
    让调用方过滤即可，日志里留一条警告。

    标题超过 500 截断；作者超过 120 截断；标题为空时退回用 URL，避免撞接口的
    ``min_length=1`` 被整篇拒收。

    :param url: 归一化后的页面 URL（也作为 ``url`` 字段原样上报）。
    :param external_id: 稳定身份（canonical URL 或其 sha256 前缀）。
    :param page: 抽取出的正文与元数据。
    :return: 请求体字典；URL 超过 500 字符时返回 ``None`` 。
    """
    if len(url) > MAX_URL_CHARS:
        logger.warning("URL 超过 %d 字符，跳过该页：%s", MAX_URL_CHARS, url[:120])
        return None

    title = (page.title or "").strip() or url
    author = (page.author or "").strip()
    return {
        "source": SOURCE,
        "external_id": external_id,
        "title": title[:MAX_TITLE_CHARS],
        "content": page.content,
        "author": author[:MAX_AUTHOR_CHARS] or None,
        "url": url,
        "doc_updated_at": _iso_or_none(page.updated_at),
    }


async def push_documents(
    client: httpx.AsyncClient,
    payloads: Iterable[dict[str, Any]],
    *,
    cfg: CrawlConfig,
    state: CrawlState,
) -> dict[str, int]:
    """
    按 ``cfg.bulk_size`` 分批推送文档，累计 ``BulkResult`` 计数。

    **单批失败不影响后续批次：** 失败批次计入返回值的 ``failed`` ，下一轮再抓；
    已成功的批次照常计数。

    :param client: 复用的 HTTP 客户端（由调用方管理生命周期）。
    :param payloads: ``build_payload`` 的产物（ ``None`` 应在上游过滤掉）。
    :param cfg: 爬虫配置（用其中的 ``api_base`` / ``bulk_size`` / 重试参数）。
    :param state: 预留参数：统计由 ``main`` 统一写，避免这里与调用方重复累加。
    :return: ``received/created/updated/duplicates/rejected/failed`` 六个计数。
    """
    # state 与 send_delete 保持同形参；计数只在 main 写一次，防止两处累加。
    _ = state

    result = {key: 0 for key in _RESULT_KEYS}
    result["failed"] = 0

    batch_size = max(1, min(int(cfg.bulk_size), MAX_BULK_DOCUMENTS))
    items = list(payloads)
    url = f"{cfg.api_base}{BULK_PATH}"

    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        response = await _post_with_retry(client, url, {"documents": batch}, cfg=cfg)

        if response is None or response.status_code >= 300:
            result["failed"] += len(batch)
            if response is not None:
                logger.error(
                    "批量推送失败：status=%s body=%s",
                    response.status_code,
                    response.text[:500],
                )
            continue

        try:
            body = response.json()
        except ValueError:
            result["failed"] += len(batch)
            logger.error("批量推送返回不是 JSON：%s", response.text[:500])
            continue

        for key in _RESULT_KEYS:
            result[key] += int(body.get(key, 0) or 0)

        for item in body.get("items", []) or []:
            if item.get("status") != "rejected":
                continue
            index = item.get("index")
            external_id = (
                batch[index]["external_id"]
                if isinstance(index, int) and 0 <= index < len(batch)
                else "?"
            )
            logger.warning("文档被拒收：external_id=%s", external_id)

    logger.info(
        "推送完成：收到 %d、新建 %d、更新 %d、重复 %d、拒收 %d、失败 %d",
        result["received"],
        result["created"],
        result["updated"],
        result["duplicates"],
        result["rejected"],
        result["failed"],
    )
    return result


async def send_delete(
    client: httpx.AsyncClient,
    *,
    cfg: CrawlConfig,
    state: CrawlState,
    external_id: str,
) -> bool | None:
    """
    发送删除信号（幂等：重复发送不会报错，第二次返回 ``False`` ）。

    **三态返回：** ``True`` 本次删掉了；``False`` 接口确认“本来就不在”（重复删除 /
    已被别处删除，同样是终态）；``None`` 表示请求没得到可用响应（网络 / 5xx / 非 JSON），
    调用方应保留本地状态、下一轮再试。把“失败”和“无需删除”区分开，否则删除接口
    短暂不可用时会永久漏撤回。

    :param client: 复用的 HTTP 客户端。
    :param cfg: 爬虫配置。
    :param state: 预留参数：删除计数由 ``main`` 统一写。
    :param external_id: 要删除的文档身份。
    :return: 是否真的删除；请求失败时返回 ``None`` 。
    """
    _ = state

    body = {"source": SOURCE, "external_id": external_id}
    response = await _post_with_retry(
        client, f"{cfg.api_base}{DELETE_PATH}", body, cfg=cfg
    )
    if response is None:
        logger.error("删除信号失败（重试耗尽）：external_id=%s", external_id)
        return None

    if response.status_code >= 300:
        logger.error(
            "删除信号失败：external_id=%s status=%s body=%s",
            external_id,
            response.status_code,
            response.text[:500],
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.error("删除接口返回不是 JSON：%s", response.text[:500])
        return None

    if not isinstance(payload, dict):
        logger.error("删除接口返回结构不对：%r", payload)
        return None

    deleted = bool(payload.get("deleted"))
    logger.info("删除信号：external_id=%s deleted=%s", external_id, deleted)
    return deleted


# ---------- 内部：带重试的 POST ----------


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    *,
    cfg: CrawlConfig,
) -> httpx.Response | None:
    """
    POST 一个 JSON body，网络异常 / 5xx / 429 时指数退避重试。

    :param client: HTTP 客户端。
    :param url: 完整地址。
    :param body: JSON 请求体。
    :param cfg: 提供 ``max_retries`` 。
    :return: 最后一次响应（含不可重试的 4xx）；重试耗尽或网络一直失败时返回 ``None`` 。
    """
    max_attempts = max(0, int(cfg.max_retries)) + 1
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        response: httpx.Response | None = None
        try:
            response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("POST %s 网络异常（第 %d 次）：%s", url, attempt, last_error)
        else:
            if response.status_code != 429 and response.status_code < 500:
                # 成功，或不可重试的 4xx —— 交给调用方判断
                return response
            last_error = f"HTTP {response.status_code}"

        if attempt >= max_attempts:
            break

        delay = _retry_delay(attempt, response)
        logger.warning(
            "POST %s 第 %d 次失败（%s），%.1fs 后重试", url, attempt, last_error, delay
        )
        await _sleep(delay)

    logger.error("POST %s 重试 %d 次仍失败：%s", url, cfg.max_retries, last_error)
    return None


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    """
    计算第 ``attempt`` 次失败后的等待时间：``1s → 2s → 4s …`` 加抖动。

    **429 优先用 ``Retry-After``** —— 服务端明确告诉你等多久，别自作聪明缩短。

    :param attempt: 第几次失败（从 1 开始）。
    :param response: 本次响应；网络异常时为 ``None`` 。
    :return: 等待秒数。
    """
    retry_after = _parse_retry_after(response) if response is not None else None
    if retry_after is not None:
        return retry_after

    base = min(2.0 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
    return base + random.uniform(0.0, 0.5)


def _parse_retry_after(response: httpx.Response) -> float | None:
    """
    解析 ``Retry-After`` 响应头（秒数或 HTTP 日期两种写法）。

    :param response: 429 响应。
    :return: 等待秒数；头缺失或解析失败时返回 ``None`` 。
    """
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
