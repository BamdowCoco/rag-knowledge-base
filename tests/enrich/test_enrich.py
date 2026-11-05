"""enrich（阶段 G）：语言 + 摘要 + 关键词 + 署名 —— 四个纯函数 + 两条联机断言。

纯函数不碰 DB / Redis / 网络（语言靠 py3langid 内置模型、关键词靠 jieba 本地词典），
所以它们不需要 fixture。
"""
from app.services.enrich_service import ENRICH_VERSION, enrich
from app.services.enricher import enrich_once
from app.utils.keywords import extract_keywords
from app.utils.language import detect_language
from app.utils.metadata import extract_author
from app.utils.summary import split_paragraphs, summarize


# ---------- detect_language ----------

def test_detects_chinese():
    assert detect_language("这是一段中文文本，用来验证语言检测能不能认出简体中文。") == "zh"


def test_detects_english():
    assert detect_language("This is a paragraph of English text, used to verify detection.") == "en"


def test_too_short_returns_none():
    assert detect_language("") is None
    assert detect_language("你好") is None          # 太短，判出来没意义
    assert detect_language("   \n  ") is None


# ---------- summarize ----------

def test_empty_returns_none():
    assert summarize("") is None
    assert summarize("   \n\n   ") is None


def test_single_paragraph_is_itself():
    assert summarize("只有一段话。") == "只有一段话。"


def test_first_and_last_paragraph():
    text = "第一段：点题。\n\n中间的细节不该进摘要。\n\n最后一段：给出结论。"
    out = summarize(text)
    assert out is not None
    assert out.startswith("第一段：点题。")
    assert out.endswith("最后一段：给出结论。")
    assert "中间的细节" not in out


def test_truncates_to_limit():
    out = summarize("首" * 100 + "\n\n" + "尾" * 100, limit=20)
    assert out is not None
    assert len(out) == 20


def test_split_paragraphs_drops_blank():
    assert split_paragraphs("a\n\n\n\n b \n\n") == ["a", "b"]


# ---------- extract_keywords ----------

def test_keywords_basic():
    out = extract_keywords("部署手册：先准备环境，再执行部署脚本，最后验证健康检查是否通过。")
    assert out                                   # 抽到了东西
    assert len(out) <= 8                         # 默认 topn
    assert all(w and w.strip() == w for w in out)
    assert len(set(out)) == len(out)             # 去重


def test_keywords_respects_topn():
    text = "环境 部署 脚本 验证 健康检查 数据库 缓存 队列 管道 切块 向量 检索"
    assert len(extract_keywords(text, topn=3)) <= 3


def test_keywords_empty_text():
    assert extract_keywords("") == []
    assert extract_keywords("   \n  ") == []


# ---------- extract_author ----------

def test_author_from_head():
    assert extract_author("作者：张三\n\n正文……") == "张三"


def test_author_variants():
    assert extract_author("文/ 李四\n正文……") == "李四"
    assert extract_author(" 撰文：王五 \n正文……") == "王五"


def test_author_in_the_middle_is_ignored():
    """
    **只看开头几行** —— 正文中间出现的"作者："很可能只是在引用别人，
    猜错了比留空更糟（NULL 是"不知道"，错值是"知道"）。
    """
    text = "\n".join(["第一行", "第二行", "第三行", "第四行", "第五行", "作者：李四"])
    assert extract_author(text) is None


def test_author_absent():
    assert extract_author("这是一篇没有任何署名的正文。") is None
    assert extract_author("") is None


# ---------- enrich 编排 ----------

async def test_enrich_fills_all_keys():
    out = await enrich({"content": "这是一段足够长的中文正文，用来验证 enrich 会填上语言、摘要和关键词。"}, {})
    assert out["language"] == "zh"
    assert out["ai_summary"]
    assert out["tags"]                       # 关键词
    assert out["enrich_version"] == ENRICH_VERSION
    assert set(out) == {"doc_type", "tags", "ai_summary", "language", "author", "enrich_version"}


async def test_enrich_fills_author_from_head_but_never_overwrites():
    text = "作者：张三\n\n这是一段足够长的正文，用来验证署名会被从开头补上，而且不会覆盖上游给的值。"
    # 调用方没给 → 从正文开头补
    assert (await enrich({"content": text}, {}))["author"] == "张三"
    # 调用方给了 → 这里返回 None，落库时 `_persist` 会回落到 raw 上那个值（不覆盖）
    assert (await enrich({"content": text}, {"author": "上游给的"}))["author"] is None


async def test_enrich_degrades_item_by_item(monkeypatch):
    """语言挂了不该连累摘要 —— 逐项降级，各自留 NULL。"""
    # 逐项降级的实现现在住在 provider 里（facade 只管分派），所以补丁打在那儿
    import app.services.enrich_providers.rule as rule_mod

    def boom(_text: str) -> str:
        raise RuntimeError("语言检测炸了")

    monkeypatch.setattr(rule_mod, "detect_language", boom)

    out = await enrich({"content": "一段足够长的正文，摘要应该照常生成出来。"}, {})
    assert out["language"] is None            # 挂掉的那项留空
    assert out["ai_summary"]                  # 另一项照常
    assert out["enrich_version"] == ENRICH_VERSION


async def test_enrich_is_filled_by_the_background_loop(client, make_doc):
    """
    联机：POST 之后 **响应里 enrich 字段还是 NULL** （异步补的必然），
    跑一轮后台补之后才是实的 —— 这正好把阶段 H 的结构改动用测试钉住。
    """
    doc = make_doc(
        source="feishu",
        external_id="enrich-1",
        content=(
            "作者：张三\n\n"
            "第一步：准备环境。\n\n"
            "最后一步：验证健康检查通过。"
        ),
    )
    created = (await client.post("/api/v1/documents", json=doc)).json()

    # ① 请求返回的那一刻：还没补（请求路径里不做 enrich）
    assert created["language"] is None
    assert created["ai_summary"] is None

    # ② 跑一轮后台补（生产里由 enrich_loop 定时跑）
    assert await enrich_once() == 1

    # ③ 再查：全都有了
    filled = (await client.get(f"/api/v1/documents/{created['id']}")).json()
    assert filled["language"] == "zh"
    assert filled["ai_summary"]
    assert "最后一步" in filled["ai_summary"]     # 尾段确实进了摘要
    assert filled["author"] == "张三"
    assert filled["tags"]                         # 关键词落到了 tags
