"""切块服务测试（纯函数：不碰 DB / Redis / 网络，只依赖 tiktoken 词表）。"""
import pytest

from app.services.chunk_service import CHUNK_OVERLAP, CHUNK_SIZE, make_chunks, split_text
from app.utils.tokenizer import count_tokens, decode_tokens, enc


def test_empty_returns_no_chunks():
    assert split_text("") == []
    assert split_text("   \n\n  ") == []
    assert split_text(None) == []


def test_short_text_is_one_chunk():
    assert split_text("只有一小段。") == ["只有一小段。"]


def test_long_text_splits_and_respects_size():
    """尺寸单位是 token —— 所以断言要比 token，不是比字符数。"""
    parts = split_text("啊" * 3000)
    assert len(parts) > 1
    assert all(count_tokens(p) <= CHUNK_SIZE for p in parts)


def test_adjacent_chunks_overlap():
    """核心：相邻块必须重叠 —— 否则被切断的语义两块都不完整。"""
    parts = split_text("这是完整的一句话。" * 100)
    assert len(parts) > 1
    assert parts[0][-20:] in parts[1]


def test_token_slicing_never_leaves_replacement_chars():
    """
     **按 token 切片会切在多字节字符中间** —— 不能把 ``�``（U+FFFD）写进块里。

    真事：一篇长中文文档的某一块 **开头** 出现过 ``�``（见 ``docs/问题记录.md``）。
    根因是 tiktoken 的 ``decode`` 对 **不完整的 UTF-8 序列** 默认填替换字符，
    而切块有两处按 token 切（overlap 取尾、无分隔符时硬切）—— 两条路都覆盖。

     **下面这组参数是实测会复现的** （当年就是这种形状）：换掉就可能抓不住了。
    """
    sentence = "进程：由于各个进程间相互隔离，一个进程的崩溃并不会影响到其他的进程的稳定性。"

    # ① 走 overlap 那条路：上一块尾部被切成下一块的开头
    parts = split_text(sentence * 11, size=32, overlap=12)
    assert len(parts) > 1
    assert "�" not in "".join(parts)

    # ② 走硬切那条路：完全没有分隔符的长串
    assert "�" not in "".join(split_text("中" * 400, size=32, overlap=8))

    # ③ 机制本身：对一个"会切坏"的 token 切片解码，也不该带出替换字符
    ids = enc.encode(sentence)
    assert "�" not in decode_tokens(ids[-12:])


def test_paragraph_boundary_preferred():
    """第一级：段落边界优先 —— 有段落时，块尾应落在完整段落上。"""
    text = "\n\n".join("段落" * 30 for _ in range(10))
    parts = split_text(text)
    assert len(parts) > 1
    assert all(p.endswith("段落") for p in parts[:-1])


def test_paragraph_longer_than_budget_falls_back_to_sentences():
    """第二级：单段超限 → 退到句子边界切。"""
    para = "。".join(["这句话不长"] * 300)          # 单个段落，远超 512 token
    parts = split_text(para)
    assert len(parts) > 1
    assert all(count_tokens(p) <= CHUNK_SIZE for p in parts)


def test_no_separator_falls_back_to_token_hard_split():
    """第三级：没有任何分隔符 → 按 token 硬切，且不抛错（此时不做重叠）。"""
    # 连续相同字符在 cl100k 里 token 效率很高（`"a"*3000` 只有 375 token），
    # 所以这里要放得足够长 —— 顺便自证「前提确实超预算」。
    text = "a" * 8000
    assert count_tokens(text) > CHUNK_SIZE
    parts = split_text(text)
    assert len(parts) > 1
    assert all(count_tokens(p) <= CHUNK_SIZE for p in parts)


def test_make_chunks_shape():
    chunks = make_chunks({"content": "啊" * 3000})
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(c["token_count"] > 0 for c in chunks)
    assert all(c["token_count"] <= CHUNK_SIZE for c in chunks)


def test_bad_params_rejected():
    with pytest.raises(ValueError):
        split_text("x", size=0)
    with pytest.raises(ValueError):
        split_text("x", size=100, overlap=100)


def test_count_tokens():
    """只断言【行为】，不写死数字 —— cl100k 对中文的切分并不直观。"""
    assert count_tokens("") == 0
    assert count_tokens("hello world") > 0
    # 确定性：同样的输入永远同样的 token 数
    assert count_tokens("中文测试" * 10) == count_tokens("中文测试" * 10)
    # 单调：文本越长 token 越多
    assert count_tokens("中" * 100) > count_tokens("中" * 10)
    # 中英都给非零
    assert count_tokens("中") >= 1


def test_make_chunks_token_count_matches_content():
    """计数缓存不能改变结果：每块的 token_count 必须等于对块内容重新编码的结果。"""
    text = "第一段。" * 50 + "\n\n" + "第二段。" * 80 + "\n\n" + "Third part. " * 60
    chunks = make_chunks({"content": text})

    assert len(chunks) > 1
    for c in chunks:
        assert c["token_count"] == count_tokens(c["content"])
        assert c["token_count"] <= CHUNK_SIZE
