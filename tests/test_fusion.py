"""RRF 融合纯函数：名次累加、去重、k 的影响、limit 与稳定排序。"""
import pytest

from app.services.fusion import rrf_fuse


def test_single_ranking_uses_reciprocal_rank():
    """单路时分数就是 ``1 / (k + rank)``，名次从 1 开始。"""
    fused = rrf_fuse([[10, 20, 30]], k=60)

    assert [chunk_id for chunk_id, _ in fused] == [10, 20, 30]
    assert fused[0][1] == pytest.approx(1 / 61)
    assert fused[1][1] == pytest.approx(1 / 62)
    assert fused[2][1] == pytest.approx(1 / 63)


def test_two_rankings_accumulate_and_reorder():
    """两路里都出现、且名次靠前的 id 得分更高（累加而不是取平均）。"""
    fused = rrf_fuse([[1, 2, 3], [3, 1]], k=60)
    scores = dict(fused)

    # id=1：两路都出现（rank 1 + rank 2）；id=3：两路都出现（rank 3 + rank 1）
    assert scores[1] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[3] == pytest.approx(1 / 63 + 1 / 61)
    assert scores[2] == pytest.approx(1 / 62)
    # id=1 与 id=3 同分时按 chunk_id 升序
    assert [chunk_id for chunk_id, _ in fused[:2]] == [1, 3]


def test_three_rankings_keep_union():
    """只出现在一路里的 id 也保留 —— 融合不是取交集。"""
    fused = rrf_fuse([[1], [2], [3]], k=60)

    assert [chunk_id for chunk_id, _ in fused] == [1, 2, 3]
    assert all(score == pytest.approx(1 / 61) for _, score in fused)


def test_duplicate_within_one_ranking_is_counted_once():
    """同一路里重复的 id 只按第一次名次计分，不能偷偷多加权。"""
    fused = rrf_fuse([[7, 7, 7]], k=60)

    assert len(fused) == 1
    assert fused[0][0] == 7
    assert fused[0][1] == pytest.approx(1 / 61)


def test_k_changes_absolute_score_but_not_order():
    """k 越小头部优势越大；绝对分数变，单路排序不变。"""
    small_k = rrf_fuse([[1, 2]], k=0)
    large_k = rrf_fuse([[1, 2]], k=1000)

    assert [chunk_id for chunk_id, _ in small_k] == [1, 2]
    assert [chunk_id for chunk_id, _ in large_k] == [1, 2]
    assert small_k[0][1] > large_k[0][1]
    # 小 k 下第一名相对第二名的优势更明显
    assert (small_k[0][1] - small_k[1][1]) > (large_k[0][1] - large_k[1][1])


def test_limit_returns_top_n():
    """``limit`` 截断在排序之后，返回的是融合后的前 N 名。"""
    fused = rrf_fuse([[3, 1, 2]], limit=2)

    assert [chunk_id for chunk_id, _ in fused] == [3, 1]


def test_ties_are_sorted_by_chunk_id():
    """完全同分时按 chunk_id 升序，保证跨进程 / 跨次运行可复现。"""
    fused = rrf_fuse([[5], [4], [3], [2]], k=60)

    assert [chunk_id for chunk_id, _ in fused] == [2, 3, 4, 5]


def test_empty_rankings():
    """空输入 / 空路都返回空列表，不抛异常。"""
    assert rrf_fuse([]) == []
    assert rrf_fuse([[], []]) == []
