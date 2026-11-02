"""
``crawler.extract`` 的单元测试：正文、标题、代码块、canonical、作者与时间。

fixture 是内联 HTML，不访问网络；需要模拟 trafilatura 缺代码块或整体失败时，
用 monkeypatch 替换主路径。
"""
from __future__ import annotations

from datetime import datetime, timezone

import crawler.extract as extract_module
from crawler.extract import extract_page

PAGE_URL = "https://example.com/zh/tutorial/dependencies/"

ARTICLE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>依赖项 - FastAPI</title>
  <link rel="canonical" href="https://example.com/zh/tutorial/dependencies/">
  <meta name="author" content="FastAPI 团队">
  <meta property="article:modified_time" content="2024-05-01T10:00:00+08:00">
</head>
<body>
  <nav>导航栏不该出现</nav>
  <header>页头不该出现</header>
  <main>
    <article class="md-content__inner md-typeset">
      <h1>依赖项</h1>
      <p>FastAPI 的依赖注入系统非常强大，同时又简单易用。它可以让开发者复用代码，减少重复逻辑。</p>
      <p>下面的命令用于安装项目依赖，请在虚拟环境中执行，避免污染全局 Python 环境。</p>
      <pre><code>pip install fastapi uvicorn</code></pre>
      <p>安装完成后运行服务，观察控制台输出，确认应用能够正常启动并响应请求。</p>
      <p>这一段再补充一些说明文字，确保正文的非空白字符数超过一百个，否则页面会被当作无效页跳过。</p>
    </article>
  </main>
  <aside>侧边栏不该出现</aside>
  <footer>页脚不该出现</footer>
  <script>console.log("脚本不该出现")</script>
</body>
</html>
"""


def test_extract_article_content_title_code_and_metadata() -> None:
    """主路径：正文、标题、代码块保留，导航/页头/侧栏/页脚/脚本被剔除。"""
    page = extract_page(ARTICLE_HTML, url=PAGE_URL)

    assert page is not None
    assert "依赖项" in page.title
    assert "依赖注入系统" in page.content
    assert "pip install fastapi uvicorn" in page.content

    for noise in ("导航栏不该出现", "页头不该出现", "侧边栏不该出现", "页脚不该出现", "脚本不该出现"):
        assert noise not in page.content

    assert page.author == "FastAPI 团队"
    assert page.canonical_url == PAGE_URL

    assert page.updated_at is not None
    assert page.updated_at.tzinfo == timezone.utc
    assert (page.updated_at.year, page.updated_at.month, page.updated_at.day) == (2024, 5, 1)


def test_extract_short_page_returns_none() -> None:
    """非空白字符数不足 ``min_chars`` 时视为无效页。"""
    html = "<html><body><article><p>太短。</p></article></body></html>"

    assert extract_page(html, url=PAGE_URL, min_chars=100) is None


def test_extract_min_chars_is_configurable() -> None:
    """``min_chars`` 可调低；短页面在阈值足够低时仍可返回。"""
    html = "<html><body><article><p>短页面也有正文。</p></article></body></html>"

    page = extract_page(html, url=PAGE_URL, min_chars=1)

    assert page is not None
    assert "短页面也有正文" in page.content


def test_extract_supplements_code_blocks_dropped_by_trafilatura(monkeypatch) -> None:
    """trafilatura 丢掉 ``<pre>`` 时，用 selectolax 的代码块补回。"""

    class _FakeDocument:
        title = "依赖项"
        author = None
        date = None
        # 模拟 2.3.x 的 txt 输出：先 YAML 元数据头，再正文；正文里没有代码块。
        text = "---\ntitle: 依赖项\nurl: https://example.com/zh/tutorial/dependencies/\n---\n" + (
            "正文内容足够长。" * 30
        )

    monkeypatch.setattr(
        extract_module,
        "_extract_with_trafilatura",
        lambda html, url: _FakeDocument(),
    )

    page = extract_page(ARTICLE_HTML, url=PAGE_URL)

    assert page is not None
    assert "正文内容足够长" in page.content
    assert "pip install fastapi uvicorn" in page.content
    # YAML 元数据头不能混进正文
    assert "hostname:" not in page.content
    assert "title: 依赖项" not in page.content


def test_extract_falls_back_to_selectolax(monkeypatch) -> None:
    """trafilatura 整体失败时，selectolax 抽取 ``article`` 并补齐 DOM 元数据。"""
    monkeypatch.setattr(
        extract_module,
        "_extract_with_trafilatura",
        lambda html, url: None,
    )

    page = extract_page(ARTICLE_HTML, url=PAGE_URL)

    assert page is not None
    assert "依赖注入系统" in page.content
    assert "pip install fastapi uvicorn" in page.content
    assert "依赖项" in page.title
    assert page.author == "FastAPI 团队"
    assert page.canonical_url == PAGE_URL
    # 主路径不可用时，时间来自 meta article:modified_time，并换算成 UTC
    assert page.updated_at == datetime(2024, 5, 1, 2, 0, tzinfo=timezone.utc)


def test_extract_fallback_removes_noise_containers(monkeypatch) -> None:
    """兜底路径选 ``body`` 时，也要移除 nav / footer 等噪声。"""
    monkeypatch.setattr(
        extract_module,
        "_extract_with_trafilatura",
        lambda html, url: None,
    )
    html = """
    <html><body>
      <nav>导航噪声</nav>
      <p>这是一段足够长的正文内容，用来验证兜底路径会移除导航与页脚，只留下正文主体。</p>
      <footer>页脚噪声</footer>
    </body></html>
    """

    page = extract_page(html, url=PAGE_URL, min_chars=10)

    assert page is not None
    assert "足够长的正文内容" in page.content
    assert "导航噪声" not in page.content
    assert "页脚噪声" not in page.content


def test_extract_ignores_cross_host_canonical() -> None:
    """canonical 指向别的 host 时不采用，避免把外部站点当成同一篇文档。"""
    html = ARTICLE_HTML.replace(
        '<link rel="canonical" href="https://example.com/zh/tutorial/dependencies/">',
        '<link rel="canonical" href="https://other.example.net/hello/">',
    )

    page = extract_page(html, url=PAGE_URL)

    assert page is not None
    assert page.canonical_url is None


def test_extract_strips_heading_anchor_marker(monkeypatch) -> None:
    """mkdocs 的标题锚点 ``¶`` 不能留在标题里（trafilatura 与 selectolax 两条路径）。"""

    class _FakeDocument:
        title = "依赖项¶"
        author = None
        date = None
        text = "正文内容足够长。" * 30

    monkeypatch.setattr(
        extract_module,
        "_extract_with_trafilatura",
        lambda html, url: _FakeDocument(),
    )

    page = extract_page(ARTICLE_HTML, url=PAGE_URL)
    assert page is not None
    assert page.title == "依赖项"

    # 主路径不可用时，selectolax 从 <h1> 读到的标题也要清理
    anchored_html = ARTICLE_HTML.replace(
        "<h1>依赖项</h1>",
        '<h1>依赖项 <a class="headerlink" href="#deps">¶</a></h1>',
    )
    monkeypatch.setattr(
        extract_module,
        "_extract_with_trafilatura",
        lambda html, url: None,
    )
    page = extract_page(anchored_html, url=PAGE_URL)
    assert page is not None
    assert page.title == "依赖项"
