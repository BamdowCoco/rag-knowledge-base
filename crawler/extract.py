"""
HTML → 正文 / 标题 / 作者 / 更新时间 / canonical 的抽取。

**两条路径：**

 - 主路径用 ``trafilatura.extract_with_metadata`` ：它对文档类页面效果好，
   还能顺带拿到标题、作者、日期
 - 兜底用 ``selectolax`` （lexbor 后端）：``article`` → ``main`` → ``body`` ，
   移除脚本、导航、页头页脚等噪声

**代码块必须保留：** 技术文档的代码示例就是正文。``trafilatura`` 在部分页面上会
丢掉 ``<pre>`` ，所以抽取后会拿 ``selectolax`` 的 ``<pre>`` 文本做比对，
缺了就补回去。

**不做完整清洗：** 全角转半角、零宽字符、HTML 实体这些都归摄入层
``app/services/normalize_service.py`` 处理；crawler 与摄入层各管一段，避免规则漂移。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import trafilatura

try:  # selectolax 1.x 只保留 lexbor 后端；这里兼容旧版路径
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:  # pragma: no cover - 仅旧版本 selectolax 会走到
    from selectolax.parser import HTMLParser  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ``trafilatura`` 的 txt 输出会在正文前加一段 YAML 元数据头；这些字段名用来
# 确认“这确实是元数据头”，避免把正文里恰好以 ``---`` 开头的分隔线误删。
_METADATA_HEADER_KEYS = (
    "title:",
    "author:",
    "url:",
    "hostname:",
    "sitename:",
    "date:",
    "fingerprint:",
)

# 兜底抽取时要清掉的噪声容器。
_NOISE_SELECTORS = ("script", "style", "nav", "header", "footer", "aside", "form")


@dataclass(frozen=True)
class ExtractedPage:
    """
    一页文档的抽取结果。

    :param title: 标题；取不到时为空串。
    :param content: 正文纯文本（保留段落与代码块换行）。
    :param author: 作者；取不到时为 ``None`` 。
    :param updated_at: 页面更新时间，统一为 UTC；取不到时为 ``None`` 。
    :param canonical_url: 页面声明的 canonical URL（与目标同 host 才接受）。
    """

    title: str
    content: str
    author: str | None
    updated_at: datetime | None
    canonical_url: str | None


def _clean_text(value: Any) -> str | None:
    """
    去掉首尾空白；空值返回 ``None`` 。

    :param value: 任意可能为字符串的值。
    :return: 清理后的字符串或 ``None`` 。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _clean_title(value: Any) -> str:
    """
    清理标题：去掉首尾空白与 mkdocs 之类主题追加的段落锚点 ``¶`` 。

    ``mkdocs-material`` 会把 ``<a class="headerlink">¶</a>`` 塞进 ``<h1>`` ，
    ``trafilatura`` 与 ``selectolax`` 都会把它读进标题。它不是正文，展示与检索都该去掉。

    :param value: 原始标题。
    :return: 清理后的标题；没有标题时为空串。
    """
    text = _clean_text(value)
    if text is None:
        return ""
    return text.rstrip("¶").strip()


def _parse_datetime(value: Any) -> datetime | None:
    """
    解析 ISO 8601 时间并统一成 UTC。

    没有时区信息的按 UTC 处理；不认识的格式返回 ``None`` 。

    :param value: 时间字符串。
    :return: 带 UTC 时区的 ``datetime`` 或 ``None`` 。
    """
    text = _clean_text(value)
    if text is None:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strip_metadata_header(text: str) -> str:
    """
    去掉 ``trafilatura`` txt 输出的 YAML 元数据头。

    2.3.x 的 ``extract_with_metadata`` 返回的 ``Document.text`` 形如
    ``---\\ntitle: ...\\n---\\n正文`` ；这段头里带着标题、URL 等字段，
    直接当正文会污染内容，也会让“短正文”被误判成长文。

    :param text: ``Document.text`` 。
    :return: 去掉元数据头后的正文。
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 3)
    if end < 0:
        return text
    header = text[4:end]
    if header and not any(line.startswith(_METADATA_HEADER_KEYS) for line in header.splitlines()):
        return text
    return text[end + len("\n---\n") :]


def _clean_content(text: str) -> str:
    """
    统一换行、去掉行尾空白，并把 3 个以上连续换行压成空一行。

    :param text: 原始正文。
    :return: 清理后的正文。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def _visible_length(text: str) -> int:
    """
    统计非空白字符数（中文按字计，和 ``CRAWL_MIN_CONTENT_CHARS`` 的口径一致）。

    :param text: 正文。
    :return: 非空白字符数。
    """
    return sum(1 for char in text if not char.isspace())


def _normalize_for_compare(text: str) -> str:
    """
    把文本压成单个空格分隔，用于“代码块是否已在正文里”的宽松比较。

    :param text: 任意文本。
    :return: 归一化后的文本。
    """
    return " ".join(text.split())


class _DomHelpers:
    """
    基于 ``selectolax`` 的 DOM 辅助读取。

    把 DOM 解析与各项小查询收在一起，``extract_page`` 只关心优先级。
    """

    def __init__(self, parser: Any) -> None:
        """
        :param parser: 已解析的 ``selectolax`` 文档树。
        """
        self.parser = parser

    def title(self) -> str | None:
        """
        取标题：优先 ``<h1>`` ，其次 ``<title>`` 。

        :return: 标题文本；都没有时返回 ``None`` 。
        """
        for selector in ("h1", "title"):
            node = self.parser.css_first(selector)
            if node is not None:
                text = _clean_text(node.text(strip=True))
                if text:
                    return text
        return None

    def meta_content(self, key: str, value: str) -> str | None:
        """
        读取 ``<meta>`` 的 ``content`` 。

        :param key: 属性名（``name`` 或 ``property``）。
        :param value: 属性值（大小写不敏感）。
        :return: ``content`` 文本；没有匹配标签时返回 ``None`` 。
        """
        target = value.lower()
        for node in self.parser.css("meta"):
            attr = node.attributes.get(key)
            if attr and attr.lower() == target:
                content = _clean_text(node.attributes.get("content"))
                if content:
                    return content
        return None

    def author(self) -> str | None:
        """
        从 ``<meta name="author">`` 取作者。

        :return: 作者文本或 ``None`` 。
        """
        return self.meta_content("name", "author")

    def updated_at(self) -> datetime | None:
        """
        兜底读取更新时间：``article:modified_time`` → ``<time datetime>`` 。

        :return: UTC 时间或 ``None`` 。
        """
        for key in ("property", "name"):
            parsed = _parse_datetime(self.meta_content(key, "article:modified_time"))
            if parsed is not None:
                return parsed
        for node in self.parser.css("time[datetime]"):
            parsed = _parse_datetime(node.attributes.get("datetime"))
            if parsed is not None:
                return parsed
        return None

    def canonical_url(self, page_url: str) -> str | None:
        """
        读取 ``<link rel="canonical">`` 并转成绝对地址。

        只接受与目标页面同 host 的 canonical，避免上游把别的站点写进来。

        :param page_url: 目标页面 URL。
        :return: 绝对 canonical URL 或 ``None`` 。
        """
        page_host = _host_of(page_url)
        for node in self.parser.css("link"):
            rel = (node.attributes.get("rel") or "").lower().split()
            if "canonical" not in rel:
                continue
            href = _clean_text(node.attributes.get("href"))
            if not href:
                continue
            absolute = urljoin(page_url, href)
            if _host_of(absolute) != page_host:
                logger.debug("忽略跨 host 的 canonical：%s（页面 host 为 %s）", absolute, page_host)
                return None
            return absolute
        return None

    def pre_texts(self) -> list[str]:
        """
        取所有 ``<pre>`` 的文本，用于补回被 ``trafilatura`` 丢掉的代码块。

        :return: 去重后的代码块文本列表。
        """
        texts: list[str] = []
        seen: set[str] = set()
        for node in self.parser.css("pre"):
            text = node.text(separator="\n", strip=False).strip()
            key = _normalize_for_compare(text)
            if key and key not in seen:
                seen.add(key)
                texts.append(text)
        return texts

    def fallback_content(self) -> str:
        """
        兜底正文：``article`` → ``main`` → ``body`` ，先移除噪声容器。

        :return: 纯文本正文；没有可用节点时返回空串。
        """
        node = self.parser.css_first("article") or self.parser.css_first("main") or self.parser.body
        if node is None:
            return ""
        for selector in _NOISE_SELECTORS:
            for noisy in node.css(selector):
                noisy.decompose()
        return node.text(separator="\n", strip=True) or ""


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


def _parse_dom(html: str) -> _DomHelpers | None:
    """
    解析 DOM；失败时返回 ``None`` ，让调用方退回 ``trafilatura`` 结果。

    :param html: 页面 HTML。
    :return: DOM 辅助对象或 ``None`` 。
    """
    try:
        return _DomHelpers(HTMLParser(html))
    except Exception:
        logger.warning("selectolax 解析 HTML 失败，跳过 DOM 兜底", exc_info=True)
        return None


def _extract_with_trafilatura(html: str, url: str) -> Any:
    """
    调用 ``trafilatura`` 主路径；异常时降级到 selectolax。

    :param html: 页面 HTML。
    :param url: 页面 URL（传给 ``trafilatura`` 辅助元数据识别）。
    :return: ``trafilatura`` 的 ``Document`` 或 ``None`` 。
    """
    try:
        return trafilatura.extract_with_metadata(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            output_format="txt",
        )
    except Exception:
        logger.warning("trafilatura 抽取失败，改用 selectolax 兜底：%s", url, exc_info=True)
        return None


def _ensure_code_blocks(content: str, pre_texts: list[str]) -> str:
    """
    把 ``trafilatura`` 漏掉的代码块补回正文末尾。

    比较时忽略空白差异；已经出现在正文里的代码块不会重复追加。

    :param content: 当前正文。
    :param pre_texts: DOM 里所有 ``<pre>`` 的文本。
    :return: 补齐代码块后的正文。
    """
    if not pre_texts:
        return content

    normalized_content = _normalize_for_compare(content)
    missing = [
        text
        for text in pre_texts
        if (key := _normalize_for_compare(text)) and key not in normalized_content
    ]

    if not missing:
        return content

    logger.debug("trafilatura 结果缺少 %d 个代码块，用 selectolax 的 <pre> 文本补回", len(missing))
    suffix = "\n\n".join(missing)
    return f"{content}\n\n{suffix}".strip() if content else suffix


def extract_page(html: str, *, url: str, min_chars: int = 100) -> ExtractedPage | None:
    """
    从 HTML 抽取一页文档。

    流程：

     - 用 ``selectolax`` 解析 DOM（取 canonical、``<pre>`` 、兜底正文）
     - 用 ``trafilatura`` 抽正文与元数据，去掉 txt 输出的 YAML 头
     - 代码块缺失时用 ``<pre>`` 文本补回
     - 非空白字符数不足 ``min_chars`` 时，尝试 selectolax 兜底；仍不足则返回 ``None``

    :param html: 页面 HTML。
    :param url: 页面 URL；用于解析相对 canonical 与 ``trafilatura`` 元数据。
    :param min_chars: 正文非空白字符数下限。
    :return: 抽取结果；正文过短或为空时返回 ``None`` 。
    """
    dom = _parse_dom(html)
    document = _extract_with_trafilatura(html, url)

    title = _clean_title(getattr(document, "title", None))
    if not title and dom is not None:
        title = _clean_title(dom.title())

    author = _clean_text(getattr(document, "author", None))
    if author is None and dom is not None:
        author = dom.author()

    updated_at = _parse_datetime(getattr(document, "date", None))
    if updated_at is None and dom is not None:
        updated_at = dom.updated_at()

    canonical_url = dom.canonical_url(url) if dom is not None else None

    content = ""
    raw_text = getattr(document, "text", None)
    if isinstance(raw_text, str) and raw_text.strip():
        content = _clean_content(_strip_metadata_header(raw_text))

    if dom is not None:
        content = _ensure_code_blocks(content, dom.pre_texts())

    threshold = max(1, min_chars)
    if _visible_length(content) < threshold and dom is not None:
        fallback = _clean_content(dom.fallback_content())
        if _visible_length(fallback) > _visible_length(content):
            content = fallback

    if _visible_length(content) < threshold:
        return None

    return ExtractedPage(
        title=title,
        content=content,
        author=author,
        updated_at=updated_at,
        canonical_url=canonical_url,
    )
