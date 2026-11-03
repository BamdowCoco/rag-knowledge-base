"""
评测指标的纯函数实现（不联网、不连库，便于单测与复用）。

口径约定：

- **按 URL 匹配证据**：同一页面的多个 chunk 视为同一证据，指标不区分 chunk；
- **URL 归一化**：去掉 fragment、去掉路径尾斜杠、scheme 与 host 转小写，
  比较前统一走 ``normalize_evidence_url``；
- **只对可回答题统计检索 / 引用指标**：``gold_urls`` 为空的题（负样本）直接跳过，
  它们的表现由 ``refusal_metrics`` 负责；
- 函数按题列表计算，多题取平均；单题调用时传长度为 1 的列表即可。
"""
import math
from urllib.parse import urlsplit, urlunsplit


def normalize_evidence_url(url: str) -> str:
    """
    归一化证据 URL，消除「同一个页面写成不同字符串」的假阴性。

    处理三件事：scheme 与 host 转小写、去掉 fragment、去掉路径末尾的斜杠。
    query 保留 —— 带 query 的页面通常确实不是同一资源，不能擅自丢弃。

    :param url: 原始 URL（``None`` / 空串按空串处理）。
    :return: 归一化后的 URL；输入为空时返回空串。
    """
    if not url:
        return ""

    parts = urlsplit(url.strip())
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


def _normalized_set(urls: list[str]) -> set[str]:
    """
    把 URL 列表转成归一化集合，顺手丢掉空串。

    :param urls: 原始 URL 列表（可含 ``None`` / 空串）。
    :return: 归一化后的非空 URL 集合。
    """
    normalized: set[str] = set()
    for url in urls:
        value = normalize_evidence_url(url)
        if value:
            normalized.add(value)
    return normalized


def _check_pairs(
    retrieved_urls: list[list[str]],
    gold_urls: list[list[str]],
) -> None:
    """
    校验「每题检索结果」与「每题 gold」两个列表等长。

    :param retrieved_urls: 每题的检索 URL 列表。
    :param gold_urls: 每题的标准证据 URL 列表。
    :raises ValueError: 两个列表长度不一致。
    """
    if len(retrieved_urls) != len(gold_urls):
        raise ValueError(
            f"retrieved_urls 与 gold_urls 题数不一致："
            f"{len(retrieved_urls)} != {len(gold_urls)}"
        )


def recall_at_k(
    retrieved_urls: list[list[str]],
    gold_urls: list[list[str]],
    k: int,
) -> float:
    """
    计算 Recall@k：top-k 中至少命中一个 gold URL 的题占比。

    只统计 ``gold_urls`` 非空的题；``k <= 0`` 或无可用题时返回 0。

    :param retrieved_urls: 每题的检索结果 URL（按相关度从高到低）。
    :param gold_urls: 每题的标准证据 URL。
    :param k: 只看前 k 条。
    :return: 0–1 之间的召回率。
    """
    _check_pairs(retrieved_urls, gold_urls)
    if k <= 0:
        return 0.0

    hits = 0
    total = 0
    for retrieved, gold in zip(retrieved_urls, gold_urls):
        gold_set = _normalized_set(gold)
        if not gold_set:
            continue
        total += 1
        top_k_set = _normalized_set(retrieved[:k])
        if top_k_set & gold_set:
            hits += 1

    return hits / total if total else 0.0


def evidence_recall_at_k(
    retrieved_urls: list[list[str]],
    gold_urls: list[list[str]],
    k: int,
) -> float:
    """
    计算 Evidence Recall@k：top-k 覆盖的 gold URL 数 / gold URL 总数，再按题平均。

    比 ``recall_at_k`` 对多证据题更敏感：命中一个只算一部分。

    :param retrieved_urls: 每题的检索结果 URL（按相关度从高到低）。
    :param gold_urls: 每题的标准证据 URL。
    :param k: 只看前 k 条。
    :return: 0–1 之间的证据召回率。
    """
    _check_pairs(retrieved_urls, gold_urls)
    if k <= 0:
        return 0.0

    values: list[float] = []
    for retrieved, gold in zip(retrieved_urls, gold_urls):
        gold_set = _normalized_set(gold)
        if not gold_set:
            continue
        covered = _normalized_set(retrieved[:k]) & gold_set
        values.append(len(covered) / len(gold_set))

    return sum(values) / len(values) if values else 0.0


def mrr(
    retrieved_urls: list[list[str]],
    gold_urls: list[list[str]],
    k: int,
) -> float:
    """
    计算 MRR（Mean Reciprocal Rank）：第一条 gold 出现位置倒数的均值。

    没有命中的题按 0 分计入均值；``gold_urls`` 为空的题跳过。

    :param retrieved_urls: 每题的检索结果 URL（按相关度从高到低）。
    :param gold_urls: 每题的标准证据 URL。
    :param k: 只在本次检索深度（前 k 条）内找第一条 gold。
    :return: 0–1 之间的 MRR。
    """
    _check_pairs(retrieved_urls, gold_urls)
    if k <= 0:
        return 0.0

    values: list[float] = []
    for retrieved, gold in zip(retrieved_urls, gold_urls):
        gold_set = _normalized_set(gold)
        if not gold_set:
            continue

        reciprocal = 0.0
        for rank, url in enumerate(retrieved[:k], start=1):
            if normalize_evidence_url(url) in gold_set:
                reciprocal = 1.0 / rank
                break
        values.append(reciprocal)

    return sum(values) / len(values) if values else 0.0


def ndcg_at_k(
    retrieved_urls: list[list[str]],
    gold_urls: list[list[str]],
    k: int = 10,
) -> float:
    """
    计算 nDCG@k（二值相关性）。

    检索列表按 URL 去重（同一页面只算一次）；DCG 用
    ``1 / log2(rank + 1)`` 折损，IDCG 用 ``min(k, gold 数)`` 个理想位置。
    ``gold_urls`` 为空的题跳过。

    :param retrieved_urls: 每题的检索结果 URL（按相关度从高到低）。
    :param gold_urls: 每题的标准证据 URL。
    :param k: 只看前 k 条（去重后的有效位次仍按前 k 条滚动计算）。
    :return: 0–1 之间的 nDCG。
    """
    _check_pairs(retrieved_urls, gold_urls)
    if k <= 0:
        return 0.0

    values: list[float] = []
    for retrieved, gold in zip(retrieved_urls, gold_urls):
        gold_set = _normalized_set(gold)
        if not gold_set:
            continue

        seen: set[str] = set()
        rank = 0
        dcg = 0.0
        for url in retrieved[:k]:
            normalized = normalize_evidence_url(url)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            rank += 1
            if normalized in gold_set:
                dcg += 1.0 / math.log2(rank + 1)

        ideal_positions = min(k, len(gold_set))
        idcg = sum(1.0 / math.log2(position + 1) for position in range(1, ideal_positions + 1))
        values.append(dcg / idcg if idcg > 0 else 0.0)

    return sum(values) / len(values) if values else 0.0


def citation_precision_recall(
    cited_urls: list[str],
    gold_urls: list[str],
) -> tuple[float, float]:
    """
    计算单题的引用准确率与引用召回率（按 URL 去重）。

    定义：

    - 准确率 = 命中的 gold URL 数 / 引用 URL 数；
    - 召回率 = 命中的 gold URL 数 / gold URL 总数。

    引用为空或 gold 为空时两项都返回 0；调用方可据此跳过该题并在报告里单列覆盖率。

    :param cited_urls: 回答实际引用的 URL。
    :param gold_urls: 本题的标准证据 URL。
    :return: ``(precision, recall)``。
    """
    cited = _normalized_set(cited_urls)
    gold = _normalized_set(gold_urls)
    if not cited or not gold:
        return 0.0, 0.0

    hit = len(cited & gold)
    return hit / len(cited), hit / len(gold)


def refusal_metrics(
    answerable: list[bool],
    refused: list[bool],
) -> dict[str, float]:
    """
    汇总拒答表现。

    - ``refusal_accuracy``：负样本中正确拒答的比例；
    - ``false_refusal_rate``：正样本被拒答的比例（误拒率）。

    某一类样本为空时，对应指标记 0（并在报告里注明样本数）。

    :param answerable: 每题是否可回答。
    :param refused: 每题是否被判定为拒答。
    :return: 含 ``refusal_accuracy`` 与 ``false_refusal_rate`` 的字典。
    :raises ValueError: 两个列表长度不一致。
    """
    if len(answerable) != len(refused):
        raise ValueError(
            f"answerable 与 refused 题数不一致：{len(answerable)} != {len(refused)}"
        )

    negatives = [flag for flag, ok in zip(refused, answerable) if not ok]
    positives = [flag for flag, ok in zip(refused, answerable) if ok]
    refused_negatives = sum(1 for flag in negatives if flag)
    refused_positives = sum(1 for flag in positives if flag)

    return {
        "refusal_accuracy": refused_negatives / len(negatives) if negatives else 0.0,
        "false_refusal_rate": refused_positives / len(positives) if positives else 0.0,
    }


def percentile(values: list[float], p: float) -> float:
    """
    计算分位数（线性插值，与 numpy 默认口径一致）。

    空列表返回 0；``p`` 超出 0–100 时按边界截断。

    :param values: 样本值（顺序不限）。
    :param p: 百分位，0–100。
    :return: 对应分位数。
    """
    if not values:
        return 0.0

    data = sorted(float(value) for value in values)
    if len(data) == 1:
        return data[0]

    clamped = min(max(float(p), 0.0), 100.0)
    position = (len(data) - 1) * clamped / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return data[lower]

    weight = position - lower
    return data[lower] * (1.0 - weight) + data[upper] * weight
