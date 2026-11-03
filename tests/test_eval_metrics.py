"""评测指标纯函数：Recall / Evidence Recall / MRR / nDCG / 引用 / 拒答 / 分位数。"""
import math

import pytest

from evals.metrics import (
    citation_precision_recall,
    evidence_recall_at_k,
    mrr,
    ndcg_at_k,
    normalize_evidence_url,
    percentile,
    recall_at_k,
    refusal_metrics,
)

A = "https://fastapi.tiangolo.com/zh/tutorial/dependencies/"
B = "https://fastapi.tiangolo.com/zh/tutorial/security/"
C = "https://fastapi.tiangolo.com/zh/tutorial/body/"


def test_normalize_url_strips_fragment_slash_and_lowercases_host():
    """fragment、尾斜杠、host 大小写都不该造成「同一页面算两条」。"""
    assert normalize_evidence_url(
        "HTTPS://FastAPI.Tiangolo.com/zh/tutorial/#dependencies"
    ) == "https://fastapi.tiangolo.com/zh/tutorial"
    assert normalize_evidence_url("https://fastapi.tiangolo.com/zh/tutorial/") == (
        "https://fastapi.tiangolo.com/zh/tutorial"
    )
    assert normalize_evidence_url("") == ""


def test_normalize_url_keeps_query():
    """query 保留：带 query 的页面通常确实不是同一资源。"""
    assert normalize_evidence_url("https://example.com/zh/doc?page=2") == (
        "https://example.com/zh/doc?page=2"
    )


def test_recall_at_k_boundary_and_miss():
    """k 边界：第 2 位命中不算 Recall@1；未命中的题计 0 但仍在分母里。"""
    retrieved = [[A, B], [C], [A]]
    gold = [[B], [C], [B]]
    assert recall_at_k(retrieved, gold, 1) == pytest.approx(1 / 3)
    assert recall_at_k(retrieved, gold, 2) == pytest.approx(2 / 3)


def test_recall_at_k_matches_after_normalization():
    """gold 带尾斜杠 / fragment 也应命中。"""
    assert recall_at_k([[A]], [["https://FASTAPI.tiangolo.com/zh/tutorial/dependencies/#x"]], 5) == 1.0


def test_recall_at_k_skips_empty_gold_and_handles_zero_k():
    """负样本（空 gold）不进入分母；k=0 视为没有检索结果。"""
    assert recall_at_k([[A], []], [[A], []], 5) == 1.0
    assert recall_at_k([[], []], [[A], []], 5) == 0.0
    assert recall_at_k([[A]], [[A]], 0) == 0.0


def test_recall_at_k_rejects_length_mismatch():
    """题数不一致属于调用方 bug，直接报错而不是静默截断。"""
    with pytest.raises(ValueError):
        recall_at_k([[A]], [[A], [B]], 5)


def test_evidence_recall_multi_gold_partial_coverage():
    """多证据题：命中 2 / 3 得 1/3（k=2 时只覆盖 1 / 3），再按题平均。"""
    assert evidence_recall_at_k([[A, B, C]], [[A, B]], 5) == 1.0
    assert evidence_recall_at_k([[A, C, B]], [[A, B]], 2) == pytest.approx(1 / 2)
    assert evidence_recall_at_k([[A, B], [C]], [[A, B], [A]], 5) == pytest.approx(
        (1.0 + 0.0) / 2
    )


def test_mrr_first_gold_rank():
    """MRR 只看第一条 gold 的排名；未命中的题按 0 计入均值。"""
    assert mrr([[A, B, C]], [[B]], 3) == pytest.approx(1 / 2)
    assert mrr([[A]], [[B]], 3) == 0.0
    assert mrr([[A, B], [C]], [[B], [A]], 3) == pytest.approx((1 / 2 + 0.0) / 2)
    assert mrr([[A], []], [[A], []], 3) == 1.0


def test_ndcg_perfect_and_miss():
    """完美命中为 1；完全未命中为 0。"""
    assert ndcg_at_k([[A]], [[A]], 10) == pytest.approx(1.0)
    assert ndcg_at_k([[A]], [[B]], 10) == 0.0


def test_ndcg_dedupes_retrieved_urls():
    """同一 URL 重复出现只算一次，位次按去重后的顺序压缩。"""
    expected = (1 / math.log2(3)) / (1 / math.log2(2))
    assert ndcg_at_k([[A, A, B]], [[B]], 10) == pytest.approx(expected)


def test_ndcg_multiple_gold_uses_min_k_idcg():
    """IDCG 用 min(k, gold 数) 个理想位置；二值相关性。"""
    dcg = 1.0 + 1 / math.log2(4)  # A 第 1 位、C 第 3 位（B 第 2 位不相关）
    idcg = 1.0 + 1 / math.log2(3)
    assert ndcg_at_k([[A, B, C]], [[A, C]], 10) == pytest.approx(dcg / idcg)


def test_ndcg_skips_empty_gold():
    """负样本不参与 nDCG。"""
    assert ndcg_at_k([[A], []], [[A], []], 10) == 1.0
    assert ndcg_at_k([[]], [[]], 10) == 0.0


def test_citation_precision_recall_basic():
    """引用准确率看被引里有多少是 gold；召回率看 gold 被覆盖多少。"""
    precision, recall = citation_precision_recall([A, B, C], [A, B])
    assert precision == pytest.approx(2 / 3)
    assert recall == pytest.approx(1.0)


def test_citation_precision_recall_dedupes_and_handles_empty():
    """重复引用去重；空引用 / 空 gold 返回 0（调用方据此跳过平均）。"""
    assert citation_precision_recall([A, A], [A]) == (1.0, 1.0)
    assert citation_precision_recall([], [A]) == (0.0, 0.0)
    assert citation_precision_recall([A], []) == (0.0, 0.0)


def test_citation_matches_after_normalization():
    """gold 与引用写法不同（尾斜杠 / host 大小写）也应算命中。"""
    precision, recall = citation_precision_recall(
        [A], ["https://FastAPI.tiangolo.com/zh/tutorial/dependencies"]
    )
    assert precision == 1.0
    assert recall == 1.0


def test_refusal_metrics_mixed():
    """负样本拒答率与正样本误拒率分开统计。"""
    result = refusal_metrics(
        answerable=[True, True, False, False],
        refused=[False, True, True, False],
    )
    assert result["false_refusal_rate"] == pytest.approx(1 / 2)
    assert result["refusal_accuracy"] == pytest.approx(1 / 2)


def test_refusal_metrics_empty_groups():
    """缺少负样本 / 正样本时对应指标记 0（报告里同时给出样本数）。"""
    only_positive = refusal_metrics([True], [False])
    assert only_positive["refusal_accuracy"] == 0.0
    assert only_positive["false_refusal_rate"] == 0.0

    only_negative = refusal_metrics([False], [True])
    assert only_negative["refusal_accuracy"] == 1.0
    assert only_negative["false_refusal_rate"] == 0.0


def test_refusal_metrics_rejects_length_mismatch():
    """两个列表必须等长。"""
    with pytest.raises(ValueError):
        refusal_metrics([True], [False, True])


def test_percentile_edges():
    """空列表、单值、边界百分位。"""
    assert percentile([], 50) == 0.0
    assert percentile([7], 95) == 7.0
    assert percentile([1, 2, 3, 4], 0) == 1.0
    assert percentile([1, 2, 3, 4], 100) == 4.0


def test_percentile_linear_interpolation_and_unsorted_input():
    """线性插值（numpy 默认口径），输入顺序不影响结果。"""
    assert percentile([4, 1, 3, 2], 50) == pytest.approx(2.5)
    assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert percentile([1, 2, 3, 4], 150) == 4.0
