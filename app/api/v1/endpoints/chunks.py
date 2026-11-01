"""
向量化任务的认领与回报（阶段 C）—— 给下游 worker 调的。
阶段 F 起同一个认领接口 **多返回一类任务** ：撤回（把过时向量从向量库删掉）。

注意路径顺序：``/chunks/claim`` （两段）与 ``/chunks/{chunk_id}/complete`` （三段）
段数不同，不会互相遮蔽。但 **将来若加 ``/chunks/{chunk_id}``，必须注册在 ``/claim`` 之后**
—— 就是阶段 7 那个 ``/{id}`` 吃 ``/stats`` 的同类坑。
"""
from fastapi import APIRouter, Body, Depends, Path

from app.api.deps import get_task_service
from app.schemas.chunk import (
    ChunkClaimRequest,
    ChunkClaimResult,
    ChunkCompleteRequest,
    ChunkCompleteResult,
    ChunkFailRequest,
    ChunkFailResult,
    ChunkRetractResult,
    ClaimedChunk,
)
from app.services.task_service import TaskService

router = APIRouter()


@router.post("/claim", response_model=ChunkClaimResult, summary="认领待向量化的分块")
async def claim_chunks(
    payload: ChunkClaimRequest,
    service: TaskService = Depends(get_task_service),
) -> ChunkClaimResult:
    """
    认领待向量化的分块，同时把过时的撤回任务一并返回。

    :param payload: 认领上限（``limit``）。
    :param service: 任务服务。
    :return: ``chunks`` 为待向量化的块，``retractions`` 为要删向量的块。
    """
    chunks = await service.claim(payload.limit)
    # 撤回是【追加】出来的一类活：旧调用方不读 retractions 也照样工作
    retractions = await service.claim_retractions(payload.limit)
    # chunks 是 dataclass（ChunkView），显式转一次，别依赖嵌套模型的隐式 from_attributes
    return ChunkClaimResult(
        chunks=[ClaimedChunk.model_validate(c) for c in chunks],
        retractions=retractions,
    )


@router.post("/{chunk_id}/complete", response_model=ChunkCompleteResult, summary="回报分块已完成")
async def complete_chunk(
    chunk_id: int = Path(..., ge=1),
    payload: ChunkCompleteRequest | None = Body(default=None),
    service: TaskService = Depends(get_task_service),
) -> ChunkCompleteResult:
    """
    回报某个分块已完成向量化。

    body 可选：不带时按旧行为完成；带 ``claim_token`` 时校验 fencing，
    并可记录产生向量的模型与维度。

    :param chunk_id: 分块 id（路径参数，``>= 1``）。
    :param payload: 可选的完成回执（``claim_token`` / ``embedding_model`` / ``embedding_dim``）。
    :param service: 任务服务。
    :return: 更新后的分块 id 与状态。
    """
    chunk = await service.complete(
        chunk_id,
        claim_token=payload.claim_token if payload else None,
        embedding_model=payload.embedding_model if payload else None,
        embedding_dim=payload.embedding_dim if payload else None,
    )
    return ChunkCompleteResult(id=chunk.id, embedding_status=chunk.embedding_status)


@router.post("/{chunk_id}/fail", response_model=ChunkFailResult, summary="回报分块向量化失败")
async def fail_chunk(
    payload: ChunkFailRequest,
    chunk_id: int = Path(..., ge=1),
    service: TaskService = Depends(get_task_service),
) -> ChunkFailResult:
    """
    回报某个分块向量化失败。

    服务端把失败次数 +1：未达上限退回 ``pending`` 并指数退避，达上限转 ``failed`` 终态。

    :param payload: 失败回执（``claim_token`` 必填、``error`` 可选）。
    :param chunk_id: 分块 id（路径参数，``>= 1``）。
    :param service: 任务服务。
    :return: 分块 id、当前状态与失败次数。
    """
    chunk = await service.fail(
        chunk_id, claim_token=payload.claim_token, error=payload.error
    )
    return ChunkFailResult(
        id=chunk.id,
        embedding_status=chunk.embedding_status,
        embedding_attempts=chunk.embedding_attempts,
    )


@router.post(
    "/{chunk_id}/retracted",
    response_model=ChunkRetractResult,
    summary="回报分块的向量已从向量库撤回",
)
async def retract_chunk(
    chunk_id: int = Path(..., ge=1),
    service: TaskService = Depends(get_task_service),
) -> ChunkRetractResult:
    """
    回报某个分块的向量已从向量库删除。

    :param chunk_id: 分块 id（路径参数，``>= 1``）。
    :param service: 任务服务。
    :return: 分块 id 与 ``retracted=True``。
    """
    chunk = await service.retract(chunk_id)
    return ChunkRetractResult(id=chunk.id, retracted=True)
