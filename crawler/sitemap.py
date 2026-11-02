"""
sitemap 解析与 URL 规范化：crawler 的“发现 URL”一步，全部是纯函数，便于单测。

**为什么用 stdlib：** ``xml.etree.ElementTree`` 足够解析 sitemap 这种极简 XML，
不必为了去 namespace 拉一个 ``lxml`` 进爬虫镜像。

**为什么在这里规范化 URL：** sitemap 里的 URL、页面上的 ``canonical`` 、Redis 里记的
``id2url`` 必须用同一口径比较，否则「连续两轮不在 sitemap 里」会被跟踪参数、
``#fragment`` 之类的差异误判成消失。
"""
from __future__ import annotations

import hashlib
import logging
import posixpath
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# 常见跟踪参数：带不带它们指向的都是同一篇文档，留着只会让 ``external_id`` 漂移。
# 比较时统一小写；``utm_*`` 按前缀匹配，其余按精确名匹配。
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset({"source", "spm", "ref", "fbclid", "gclid"})

# 只能用来判断“不是文档页”的静态资源后缀；不在表里的未知后缀仍按 HTML 处理，
# 避免把没有后缀或使用自定义后缀的文档页误杀。
_NON_HTML_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".pdf",
        ".zip",
        ".gz",
        ".tar",
        ".css",
        ".js",
        ".xml",
        ".json",
        ".woff",
        ".woff2",
        ".ico",
        ".mp4",
        ".mp3",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}

# 现有接口对 ``external_id`` 的长度上限；超长时退回哈希。
_EXTERNAL_ID_MAX_LEN = 200
_EXTERNAL_ID_HASH_LEN = 40


@dataclass(frozen=True)
class SitemapUrl:
    """
    sitemap 里的一个条目。

    :param loc: ``<loc>`` 原文，尚未规范化。
    :param lastmod: 解析后的 ``<lastmod>`` ；缺失或格式不认识时为 ``None`` 。
    """

    loc: str
    lastmod: datetime | None = None


@dataclass(frozen=True)
class SitemapDoc:
    """
    一个 sitemap 文件的解析结果。

    :param urls: ``urlset`` 里的页面条目；``sitemapindex`` 时为空。
    :param sitemaps: ``sitemapindex`` 里的子 sitemap 地址；``urlset`` 时为空。
    """

    urls: list[SitemapUrl]
    sitemaps: list[str]


def _local_name(tag: str) -> str:
    """
    去掉 ElementTree 展开后的 namespace 前缀。

    :param tag: 形如 ``{http://www.sitemaps.org/schemas/sitemap/0.9}url`` 的标签。
    :return: 不带 namespace 的本地名。
    """
    return tag.rsplit("}", 1)[-1]


def _child_text(node: ET.Element, name: str) -> str | None:
    """
    取直接子元素的文本（空串视为缺失）。

    :param node: 父元素。
    :param name: 去 namespace 后的子元素名。
    :return: 去空白后的文本；没有该子元素或内容为空时返回 ``None`` 。
    """
    for child in node:
        if _local_name(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def _parse_lastmod(value: str | None) -> datetime | None:
    """
    解析 sitemap 的 ``<lastmod>`` ，统一成 UTC。

    sitemap 协议用的是 W3C DATETIME，常见形式为 ``2024-05-01`` 或
    ``2024-05-01T12:00:00+00:00`` 。没有时区信息的按 UTC 处理。

    :param value: ``<lastmod>`` 原文。
    :return: UTC 时间；缺失或格式不认识时为 ``None`` 。
    """
    if not value:
        return None
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        logger.debug("无法解析 sitemap lastmod：%r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_sitemap(xml_bytes: bytes) -> SitemapDoc:
    """
    解析 ``urlset`` 或 ``sitemapindex`` ，不依赖 namespace 前缀。

    :param xml_bytes: sitemap 的原始字节。
    :return: 页面条目与子 sitemap 地址。
    :raises ValueError: 根元素既不是 ``urlset`` 也不是 ``sitemapindex`` 。
    :raises xml.etree.ElementTree.ParseError: XML 本身无法解析。
    """
    root = ET.fromstring(xml_bytes)
    root_name = _local_name(root.tag)

    urls: list[SitemapUrl] = []
    sitemaps: list[str] = []

    if root_name == "sitemapindex":
        for node in root:
            if _local_name(node.tag) != "sitemap":
                continue
            loc = _child_text(node, "loc")
            if loc:
                sitemaps.append(loc)
    elif root_name == "urlset":
        for node in root:
            if _local_name(node.tag) != "url":
                continue
            loc = _child_text(node, "loc")
            if not loc:
                continue
            urls.append(SitemapUrl(loc=loc, lastmod=_parse_lastmod(_child_text(node, "lastmod"))))
    else:
        raise ValueError(f"不支持的 sitemap 根元素：{root_name!r}")

    return SitemapDoc(urls=urls, sitemaps=sitemaps)


def _is_tracking_param(name: str) -> bool:
    """
    判断查询参数是否属于需要丢弃的跟踪参数。

    :param name: 查询参数名（原样，比较时转小写）。
    :return: 是跟踪参数时为 ``True`` 。
    """
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """
    把 URL 规范化成可稳定比较的形式。

    规则：

     - 只保留 ``http`` / ``https`` ；其他 scheme 返回空串，由调用方丢弃
     - scheme 与 host 小写，去掉 ``:80`` / ``:443`` 这类默认端口
     - 去 ``#fragment``
     - 去掉 ``utm_*`` / ``source`` / ``spm`` / ``ref`` / ``fbclid`` / ``gclid``
     - 剩余 query 按参数名、值排序
     - **保留尾斜杠**（与目标站 canonical 的口径一致）

    :param url: 原始 URL，允许带前后空白。
    :return: 规范化后的 URL；无法识别为 http(s) 绝对地址时返回空串。
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return ""

    host = (parts.hostname or "").lower()
    if not host:
        return ""

    try:
        port = parts.port
    except ValueError:
        # 端口不是数字（``https://example.com:abc/``）：当作无效 URL。
        return ""

    # IPv6 的 hostname 不带方括号，拼回 netloc 时要补上。
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"

    # 空 path 统一成 ``/`` ；非空 path 原样保留（含尾斜杠）。
    path = parts.path or "/"

    query_pairs = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    query_pairs.sort()
    query = urlencode(query_pairs)

    return urlunsplit((scheme, netloc, path, query, ""))


def is_html_url(url: str) -> bool:
    """
    判断 URL 是否可能指向 HTML 页面。

    只按路径后缀排除计划里列出的静态资源；没有后缀或未知后缀一律当作 HTML，
    避免误杀无后缀的文档页。

    :param url: 待判断的 URL。
    :return: 可能返回 HTML 时为 ``True`` 。
    """
    path = urlsplit(url).path or "/"
    suffix = posixpath.splitext(path.lower())[1]
    return suffix not in _NON_HTML_SUFFIXES


def filter_urls(
    urls: list[SitemapUrl],
    *,
    prefix: str,
    max_pages: int,
    allowed: Callable[[str], bool] | None = None,
) -> list[str]:
    """
    从 sitemap 条目里筛出本轮要抓的页面。

    顺序固定为：前缀 → HTML → ``allowed`` → 规范化去重（保序）→ ``max_pages`` 截断。

    :param urls: ``parse_sitemap`` 得到的条目。
    :param prefix: URL 前缀（如 ``https://fastapi.tiangolo.com/zh/`` ）。
    :param max_pages: 最多返回多少条；``<= 0`` 时不抓。
    :param allowed: 额外过滤（例如 robots 允许），入参是规范化后的 URL。
    :return: 规范化、去重后的 URL 列表。
    """
    if max_pages <= 0:
        return []

    prefix_raw = (prefix or "").strip()
    normalized_prefix = normalize_url(prefix_raw) or prefix_raw
    if not normalized_prefix:
        return []

    result: list[str] = []
    seen: set[str] = set()
    for item in urls:
        normalized = normalize_url(item.loc)
        if not normalized or not normalized.startswith(normalized_prefix):
            continue
        if not is_html_url(normalized):
            continue
        if allowed is not None and not allowed(normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= max_pages:
            break
    return result


def make_external_id(url: str) -> str:
    """
    生成稳定、长度可控的 ``external_id`` 。

    短 URL 直接原样使用（可读，便于排查）；超过 200 字符时用
    ``sha256(url)[:40]`` ，完整 URL 仍由调用方写入 ``url`` 字段。

    :param url: 规范化后的 canonical URL。
    :return: 长度不超过 200 的稳定 ID。
    """
    if len(url) <= _EXTERNAL_ID_MAX_LEN:
        return url
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:_EXTERNAL_ID_HASH_LEN]
