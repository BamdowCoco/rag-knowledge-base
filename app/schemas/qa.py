"""检索 / 问答 / 反馈的出入参。

检索与问答是 **只读** 的一侧（``APP_ROLE=rag``）：它们不写 MySQL，只把 Qdrant 的召回
与 MySQL 的正文拼成结果。所以这里的模型都只描述「读出来的形状」。
"""
from typing import Literal

from pydantic import BaseModel, Field


class SearchFilters(BaseModel):
    """检索过滤条件 —— 对应 Qdrant payload 里的可过滤字段。

    所有字段为空时等价于「不过滤」。``tags`` 用的是「命中任意一个即可」（``MatchAny``），
    与 ``source`` / ``document_ids`` 的语义一致。
    """

    source: list[str] | None = None
    language: str | None = None
    tags: list[str] | None = None
    document_ids: list[int] | None = None


class SearchRequest(BaseModel):
    """检索请求体。

    三个可选覆盖字段都带默认值，老客户端不必改请求就能继续用：

    - ``retrieval_mode``：按次覆盖服务端的 ``RETRIEVAL_MODE``；
    - ``use_cache``：评测传 ``False``，连 query embedding 缓存也一并关掉，保证可复现；
    - ``score_threshold``：**检索接口本身不拒答**，该字段只为与 ``/chat/stream``
      保持同一套请求形状而接收，``/search`` 会忽略它。
    """

    # 上限 2000 字符：query 要送去做 embedding，不设上限等于把成本交给调用方
    query: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(5, ge=1, le=50)
    filters: SearchFilters | None = None
    retrieval_mode: Literal["dense", "hybrid"] | None = None
    use_cache: bool = True
    score_threshold: float | None = None


class RetrievalResult(BaseModel):
    """一条检索结果：向量分数 + 回表拿到的正文与元数据。"""

    chunk_id: int
    document_id: int
    title: str
    source: str
    url: str | None = None
    content: str
    score: float
    chunk_index: int
    language: str | None = None
    tags: list[str] | None = None


class SearchResponse(BaseModel):
    """检索响应：结果列表 + 耗时 + 本次使用的 embedding 模型。

    新增字段让调用方（评测脚本 / 前端）能判断这次到底走了哪条路径：

    - ``retrieval_mode``：实际使用的模式（关键词索引缺失时会从 hybrid 退回 dense）；
    - ``score_type``：``cosine``（dense）或 ``rrf``（hybrid 融合分）；
    - ``best_dense_score``：dense 候选第一条的余弦分，也是问答拒答阈值的比较对象；
    - ``keyword_hits``：关键词一路返回的条数（dense 模式恒为 0）。
    """

    results: list[RetrievalResult]
    latency_ms: int
    embedding_model: str
    retrieval_mode: str = "dense"
    score_type: Literal["cosine", "rrf"] = "cosine"
    best_dense_score: float = 0.0
    keyword_hits: int = 0


class ChatRequest(BaseModel):
    """流式问答请求体。

    与 ``SearchRequest`` 一样支持按次覆盖：``retrieval_mode`` / ``use_cache`` /
    ``score_threshold``。``score_threshold`` 会覆盖服务端阈值，用于评测集校准；
    传 ``None`` 时仍读 ``settings.retrieval_score_threshold``。
    """

    question: str = Field(..., min_length=1, max_length=2000)
    top_k: int = Field(5, ge=1, le=50)
    filters: SearchFilters | None = None
    # 目前只回显给前端做会话关联，服务端不保存多轮上下文
    session_id: str | None = None
    retrieval_mode: Literal["dense", "hybrid"] | None = None
    use_cache: bool = True
    score_threshold: float | None = None


class Citation(BaseModel):
    """回答里 ``[n]`` 对应的资料条目。"""

    n: int
    chunk_id: int
    document_id: int
    title: str
    source: str
    url: str | None = None


class FeedbackRequest(BaseModel):
    """用户反馈（点赞 / 点踩 + 可选评论）。

    ``question_hash`` / ``answer_id`` 都由前端回传，服务端只做追加写入，不做关联查询。
    """

    question_hash: str | None = None
    answer_id: str | None = None
    rating: Literal["up", "down"]
    comment: str | None = Field(None, max_length=2000)


class FeedbackResult(BaseModel):
    """反馈写入结果（best-effort：写失败也返回 ``ok=True``）。"""

    ok: bool
