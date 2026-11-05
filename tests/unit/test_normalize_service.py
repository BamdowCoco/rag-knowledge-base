"""标准化服务测试（纯函数，无需 DB / Redis / 网络）。"""
from app.services.normalize_service import normalize_document, normalize_content, normalize_title


def test_strips_script_and_style_blocks():
    raw = "<p>正文</p><script>alert('x')</script><style>.a{color:red}</style>"
    out = normalize_content(raw)
    assert "alert" not in out and "color" not in out and "正文" in out


def test_removes_html_tags():
    assert normalize_content("<p>hello <b>world</b></p>") == "hello world"


def test_unescape_after_tag_strip():
    # &lt;script&gt; 是「正文里的字面量」，不该在去标签阶段被当成真标签删掉
    assert normalize_content("&lt;script&gt;不是真标签&lt;/script&gt;") == "<script>不是真标签</script>"


def test_fullwidth_to_halfwidth():
    assert normalize_content("ＡＢＣ１２３　全角空格") == "ABC123 全角空格"


def test_zero_width_removed():
    assert normalize_content("a\u200bb\ufeffc") == "abc"


def test_title_collapses_to_single_line():
    assert normalize_title("  第一行\n\n第二行  ") == "第一行 第二行"


def test_content_keeps_paragraphs():
    assert normalize_content("<p>第一段</p><p>第二段</p>") == "第一段\n第二段"


def test_truncation():
    assert len(normalize_title("标" * 300)) == 255
    assert len(normalize_content("字" * 30000)) == 20000


def test_guard_non_str():
    assert normalize_content(None) == ""
    assert normalize_content(123) == ""
    assert normalize_title(None) == ""


def test_hash_stable_across_raw_whitespace():
    """核心：原始空白不同 → 标准化后相同 → 哈希相同（否则判重失效）。"""
    a = normalize_document({"title": "标题 ", "content": "正文   内容"})
    b = normalize_document({"title": " 标题", "content": "正文 内容"})
    assert a["content_hash"] == b["content_hash"]


def test_hash_ignores_markup():
    a = normalize_document({"title": "标题", "content": "<p>正文</p>"})
    b = normalize_document({"title": "标题", "content": "正文"})
    assert a["content_hash"] == b["content_hash"]


def test_hash_differs_for_different_content():
    a = normalize_document({"title": "标题", "content": "正文A"})
    b = normalize_document({"title": "标题", "content": "正文B"})
    assert a["content_hash"] != b["content_hash"]
