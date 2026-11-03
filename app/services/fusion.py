"""
检索结果融合：Reciprocal Rank Fusion（RRF）。

**为什么不用分数加权和。** 向量检索给的是余弦相似度（大致 0–1），MySQL
``MATCH ... AGAINST`` 给的是与词频 / 逆文档频率相关的关键词分（量纲完全不同，
分布也随语料变化）。把两个不同量纲的数直接相加，等于让「哪一路的分数量级更大」
决定排序 —— 权重每次换语料都要重新拍，不可解释也不可复现。

RRF 只用 **排名** ，完全绕开分数可比性问题：

.. code-block:: text

    rrf(d) = Σ_i 1 / (k + rank_i(d))

其中 ``rank_i(d)`` 从 1 开始，``k`` 是平滑常数（常用 60）。出现在多路结果里、
且名次靠前的文档自然得分更高。它的代价是丢掉了分数差（第 1 和第 2 名只差
1/(k+1) - 1/(k+2)），但换来了跨检索器稳定、无需调权重的融合。
"""


def rrf_fuse(
    rankings: list[list[int]],
    *,
    k: int = 60,
    limit: int | None = None,
) -> list[tuple[int, float]]:
    """
    把多路有序结果融合成一路 ``(chunk_id, rrf_score)`` 列表。

    规则：

    - 每一路按 **名次从 1 开始** 累加 ``1 / (k + rank)``；
    - 同一路里重复出现的 id 只记第一次（否则重复项会被多算一次，等于偷偷加权）；
    - 同分按 ``chunk_id`` 升序，保证结果可复现（Python 的排序本身稳定，
      但上游列表顺序可能有随机性，显式加 id 兜底）；
    - 只出现在一路里的 id 也保留（融合不是取交集）。

    :param rankings: 多路候选，每路是按名次排好的 chunk id 列表。
    :param k: 平滑常数；越大越平缓，头部名次的优势越小。
    :param limit: 只返回前多少条；``None`` 表示全部返回。
    :return: 按 RRF 分数降序、同分 id 升序的 ``(chunk_id, score)`` 列表。
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        seen: set[int] = set()
        for rank, chunk_id in enumerate(ranking, start=1):
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)

    fused = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if limit is not None:
        return fused[:limit]
    return fused
