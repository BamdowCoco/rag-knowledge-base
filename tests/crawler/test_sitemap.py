"""
``crawler.sitemap`` 的单元测试：解析、URL 规范化、过滤与 ``external_id`` 。

全部是纯函数测试，不碰网络。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from crawler.sitemap import (
    SitemapUrl,
    filter_urls,
    is_html_url,
    make_external_id,
    normalize_url,
    parse_sitemap,
)


def test_parse_urlset_with_namespace() -> None:
    """``urlset``：去 namespace、读 ``loc`` 与 ``lastmod``，时间统一成 UTC。"""
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url>
        <loc>https://example.com/zh/a/</loc>
        <lastmod>2024-05-01T10:00:00+08:00</lastmod>
      </url>
      <url>
        <loc>https://example.com/zh/b/</loc>
      </url>
    </urlset>
    """

    doc = parse_sitemap(xml)

    assert doc.sitemaps == []
    assert [item.loc for item in doc.urls] == [
        "https://example.com/zh/a/",
        "https://example.com/zh/b/",
    ]
    assert doc.urls[0].lastmod == datetime(2024, 5, 1, 2, 0, tzinfo=timezone.utc)
    assert doc.urls[1].lastmod is None


def test_parse_urlset_with_prefixed_namespace() -> None:
    """带前缀的 namespace（``sm:``）也能按本地名识别。"""
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sm:urlset xmlns:sm="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sm:url><sm:loc>https://example.com/zh/a/</sm:loc></sm:url>
    </sm:urlset>
    """

    doc = parse_sitemap(xml)

    assert [item.loc for item in doc.urls] == ["https://example.com/zh/a/"]


def test_parse_sitemapindex() -> None:
    """``sitemapindex``：只返回子 sitemap 地址，不产生页面条目。"""
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
      <sitemap><loc>https://example.com/sitemap-2.xml</loc></sitemap>
    </sitemapindex>
    """

    doc = parse_sitemap(xml)

    assert doc.urls == []
    assert doc.sitemaps == [
        "https://example.com/sitemap-1.xml",
        "https://example.com/sitemap-2.xml",
    ]


def test_parse_sitemap_rejects_unknown_root() -> None:
    """根元素不是 ``urlset`` / ``sitemapindex`` 时抛 ``ValueError`` 。"""
    with pytest.raises(ValueError):
        parse_sitemap(b"<rss><channel/></rss>")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # scheme 与 host 小写，path 大小写保留
        ("HTTPS://Example.COM/Path/#frag", "https://example.com/Path/"),
        # 去默认端口
        ("https://Example.com:443/zh/", "https://example.com/zh/"),
        ("http://Example.com:80/zh/", "http://example.com/zh/"),
        # 非默认端口保留
        ("http://example.com:8080/zh/", "http://example.com:8080/zh/"),
        # 空 path 统一成 /
        ("https://example.com", "https://example.com/"),
        # fragment 去掉
        ("https://example.com/zh/page#section", "https://example.com/zh/page"),
        # 保留尾斜杠（与站点 canonical 口径一致）
        ("https://example.com/zh/page/", "https://example.com/zh/page/"),
        # 跟踪参数全部去掉
        (
            "https://example.com/zh/?utm_source=x&utm_medium=y&source=z&spm=1&ref=r&fbclid=b&gclid=g",
            "https://example.com/zh/",
        ),
        # 跟踪参数大小写不敏感
        ("https://example.com/zh/?UTM_Source=x&Page=2", "https://example.com/zh/?Page=2"),
        # 剩余 query 排序
        ("https://example.com/zh/?z=1&a=2", "https://example.com/zh/?a=2&z=1"),
        # 非 http(s) 地址丢弃
        ("ftp://example.com/zh/", ""),
        ("/zh/relative/", ""),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    """``normalize_url`` 的逐条规则。"""
    assert normalize_url(raw) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/zh/tutorial/dependencies/", True),
        ("https://example.com/zh/page", True),
        ("https://example.com/zh/page.html", True),
        ("https://example.com/zh/page.HTML", True),
        ("https://example.com/zh/image.png", False),
        ("https://example.com/zh/image.PNG", False),
        ("https://example.com/zh/doc.pdf", False),
        ("https://example.com/static/app.js", False),
        ("https://example.com/static/app.css", False),
        ("https://example.com/sitemap.xml", False),
        ("https://example.com/api/data.json", False),
        # 未知后缀不误杀
        ("https://example.com/zh/page.php", True),
    ],
)
def test_is_html_url(url: str, expected: bool) -> None:
    """静态资源后缀被排除，未知后缀按 HTML 处理。"""
    assert is_html_url(url) is expected


def test_filter_urls_prefix_html_allowed_dedupe_and_limit() -> None:
    """过滤顺序：前缀 → HTML → ``allowed`` → 去重保序 → ``max_pages`` 。"""
    entries = [
        SitemapUrl("https://Example.com/zh/a/#frag"),
        SitemapUrl("https://example.com/zh/a/?utm_source=x"),
        SitemapUrl("https://example.com/zh/b/"),
        SitemapUrl("https://example.com/en/c/"),
        SitemapUrl("https://example.com/zh/image.png"),
        SitemapUrl("https://example.com/zh/blocked/"),
    ]

    result = filter_urls(
        entries,
        prefix="https://example.com/zh/",
        max_pages=10,
        allowed=lambda url: "blocked" not in url,
    )

    assert result == ["https://example.com/zh/a/", "https://example.com/zh/b/"]

    limited = filter_urls(
        entries,
        prefix="https://example.com/zh/",
        max_pages=1,
        allowed=lambda url: "blocked" not in url,
    )
    assert limited == ["https://example.com/zh/a/"]


def test_filter_urls_empty_prefix_returns_empty() -> None:
    """空前缀视为不抓，避免配置缺失时误抓全站。"""
    assert filter_urls([SitemapUrl("https://example.com/zh/a/")], prefix="", max_pages=10) == []


def test_make_external_id_short_url_is_unchanged() -> None:
    """200 字符以内直接原样返回，便于排查。"""
    url = "https://example.com/zh/tutorial/dependencies/"
    assert make_external_id(url) == url

    boundary = "https://e.com/" + "a" * (200 - len("https://e.com/"))
    assert len(boundary) == 200
    assert make_external_id(boundary) == boundary


def test_make_external_id_long_url_is_stable_sha256_prefix() -> None:
    """超过 200 字符时返回 40 位 hex，且同一 URL 每次都一样。"""
    url = "https://example.com/zh/" + "a" * 300

    first = make_external_id(url)
    second = make_external_id(url)

    assert len(first) == 40
    assert all(char in "0123456789abcdef" for char in first)
    assert first == second
    assert first != make_external_id(url + "b")
