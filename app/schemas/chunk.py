"""分块相关的出入参。"""
from pydantic import BaseModel, ConfigDict, Field


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    content: str
    token_count: int
    embedding_status: str


class ChunkClaimRequest(BaseModel):
    """worker 认领任务的请求体。"""
    limit: int = Field(20, ge=1, le=200)


class ClaimedChunk(BaseModel):
    """
    认领到的任务单元 —— chunk 本体 + 所属版本的元数据。

    元数据来自 ``document_chunk JOIN document_clean``：worker 不用再查 MySQL，
    就能直接构造 Qdrant payload。``claim_token`` 必须在 complete / fail 时回传。
    """
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    content: str
    token_count: int
    claim_token: str
    source: str
    external_id: str | None
    title: str
    url: str | None
    language: str | None
    tags: list[str] | None
    content_hash: str
    clean_version: int


class ChunkCompleteRequest(BaseModel):
    """回报完成的请求体（可选）。不带 body 时保持旧行为。"""
    # 带 token 时服务端校验 fencing；旧调用方可以继续不带
    claim_token: str | None = None
    # 向量由哪个模型 / 维度产生 —— 记下来供重嵌入与检索侧过滤
    embedding_model: str | None = None
    embedding_dim: int | None = None


class RetractionItem(BaseModel):
    """要下游从向量库删掉的块 —— 只需要 id 与它属于哪篇文档。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int


class ChunkClaimResult(BaseModel):
    chunks: list[ClaimedChunk]
    # 追加字段（对旧调用方是向后兼容的）：这些块的所属版本已作废，
    # 下游该把它们对应的向量从向量库里删掉。见「撤回流」。
    retractions: list[RetractionItem] = []


class ChunkCompleteResult(BaseModel):
    id: int
    embedding_status: str


class ChunkFailRequest(BaseModel):
    """回报失败的请求体 —— token 必填，error 只用于排查、截断后入库。"""
    claim_token: str = Field(..., min_length=1)
    error: str | None = Field(None, max_length=2000)


class ChunkFailResult(BaseModel):
    """失败回报的结果：当前状态 + 已经失败了几次。"""
    id: int
    embedding_status: str
    embedding_attempts: int


class ChunkRetractResult(BaseModel):
    """回报「该向量已从向量库删掉」。"""
    id: int
    retracted: bool
