"""
真实向量化 worker：认领 chunk → embedding → Qdrant upsert → 回报；撤回 → Qdrant delete → 回报。

【写序是硬约束】先 ``upsert(wait=True)`` 成功、再调 ``complete``：

- Qdrant 写失败却先报了完成，MySQL 会以为「已向量化」，这条就再也不会被重试；
- 反过来（Qdrant 写成功、complete 失败）最坏只是重复写一次 —— upsert 按
  ``point id = chunk_id`` **幂等** ，重写同一批不会产生重复向量。

删除同理：先 ``delete(wait=True)`` 再 ``retracted``。删除也是幂等的，所以「回报失败、
下一轮重试」不会出问题 —— 只是多删一次。

【为什么主循环永不退出】worker 是常驻进程：没活干就 sleep 轮询，单轮异常（api 抖动、
上游超时）只影响这一轮，不能把进程打死。真正的「毒丸」判定在服务端（失败次数到上限转
``failed``），worker 只负责如实回报。

【每批一个事务边界】embedding 按 ``EMBED_BATCH_SIZE`` 分批：一批可以一次模型推理
（GPU/ONNX 利用率高），失败时也只把这一批标失败，不会让整次认领一起回滚。
"""
import asyncio
import logging
import os

import httpx
from qdrant_client import AsyncQdrantClient, models

from app.core.config import settings
from app.infra.qdrant_client import DENSE_VECTOR_NAME, ensure_collection, get_qdrant
from app.services.embedding_providers import get_provider
from app.utils.time import utcnow

# 都走环境变量：compose 里可调，改一个 worker 的节奏不必重建镜像
API_BASE = os.getenv("API_BASE", "http://api:8000")
CLAIM_LIMIT = int(os.getenv("CLAIM_LIMIT", "20"))
# 批大小默认跟随应用配置（rag / embedder 同一份 Settings），也可用环境变量单独覆盖
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", str(settings.embedding_batch_size)))
IDLE_SLEEP = float(os.getenv("IDLE_SLEEP", "2"))        # 没活干时的轮询间隔

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [embedder] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("embedder")


def _build_payload(chunk: dict, provider) -> dict:
    """
    构造 Qdrant point 的 payload。

    **不存正文** —— 正文的真相在 MySQL；这里只放检索要用的过滤字段与展示元数据，
    保证向量库可以被随时重建而不丢信息。

    :param chunk: 认领接口返回的块（含所属版本元数据）。
    :param provider: embedding provider（提供 ``model`` / ``dim``）。
    :return: 可直接写入 Qdrant 的 payload 字典。
    """
    return {
        "chunk_id": chunk["id"],
        "document_id": chunk["document_id"],
        "source": chunk["source"],
        "external_id": chunk.get("external_id"),
        "url": chunk.get("url"),
        "title": chunk["title"],
        "language": chunk.get("language"),
        "tags": chunk.get("tags") or [],
        "chunk_index": chunk["chunk_index"],
        "content_hash": chunk["content_hash"],
        "clean_version": chunk["clean_version"],
        "embedding_model": provider.model,
        "embedding_dim": provider.dim,
        "indexed_at": utcnow().isoformat(),
    }


async def _report_fail(client: httpx.AsyncClient, chunk: dict, exc: Exception) -> None:
    """
    回报单块失败（best-effort：回报本身失败只记日志，下一轮由服务端回收重试）。

    :param client: 指向 ingest api 的 HTTP 客户端。
    :param chunk: 认领接口返回的块。
    :param exc: 导致失败的异常。
    """
    try:
        response = await client.post(
            f"/api/v1/chunks/{chunk['id']}/fail",
            json={"claim_token": chunk["claim_token"], "error": str(exc)[:2000]},
        )
        response.raise_for_status()
    except Exception as report_exc:  # noqa: BLE001 - 回报失败不影响下一批
        logger.warning("回报 chunk %s 失败时出错：%s", chunk["id"], report_exc)


async def _complete_chunk(client: httpx.AsyncClient, chunk: dict, provider) -> None:
    """
    回报单块完成（Qdrant 已 ``wait=True`` 写成功之后才调）。

    :param client: 指向 ingest api 的 HTTP 客户端。
    :param chunk: 认领接口返回的块。
    :param provider: embedding provider（记录模型 / 维度）。
    :raises httpx.HTTPStatusError: 回报失败（例如 token 已被回收，409）。
    """
    response = await client.post(
        f"/api/v1/chunks/{chunk['id']}/complete",
        json={
            "claim_token": chunk["claim_token"],
            "embedding_model": provider.model,
            "embedding_dim": provider.dim,
        },
    )
    response.raise_for_status()


async def index_once(
    client: httpx.AsyncClient,
    qdrant: AsyncQdrantClient,
    provider,
    *,
    limit: int | None = None,
) -> dict[str, int]:
    """
    跑一轮：认领一批任务，做 embedding 并写向量库，再回报。

    处理顺序：先补齐（chunks），再撤回（retractions）。两者互不依赖，
    先补后删只是习惯 —— 同一轮里不会出现「刚写又删」的情况（作废的块不会再被认领）。

    :param client: 指向 ingest api 的 HTTP 客户端。
    :param qdrant: Qdrant 客户端。
    :param provider: embedding provider。
    :param limit: 本轮认领上限；``None`` 用 ``CLAIM_LIMIT``。
    :return: ``{"done", "failed", "retracted", "claimed"}`` 四个计数。
    """
    response = await client.post(
        "/api/v1/chunks/claim", json={"limit": limit or CLAIM_LIMIT}
    )
    response.raise_for_status()
    body = response.json()

    chunks: list[dict] = body.get("chunks", [])
    retractions: list[dict] = body.get("retractions", [])
    stats = {
        "done": 0,
        "failed": 0,
        "retracted": 0,
        "complete_failed": 0,
        "claimed": len(chunks),
    }

    # ---------- 补齐：embedding + upsert + complete ----------
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        try:
            vectors = await provider.embed([chunk["content"] for chunk in batch])
            # 数量对不上就整批失败：宁可重试，也不要把 A 的向量写到 B 上（顺序契约）
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"embedding 返回 {len(vectors)} 条向量，本批有 {len(batch)} 块"
                )
            points = [
                models.PointStruct(
                    id=chunk["id"],
                    vector={DENSE_VECTOR_NAME: vector},
                    payload=_build_payload(chunk, provider),
                )
                for chunk, vector in zip(batch, vectors)
            ]
            # wait=True：等 Qdrant 真正落盘再回报，否则「完成」可能指向还不存在的向量
            await qdrant.upsert(
                collection_name=settings.qdrant_collection,
                points=points,
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001 - 整批失败，逐块回报失败继续下一批
            logger.warning("批处理失败（%d 块）：%s", len(batch), exc)
            for chunk in batch:
                await _report_fail(client, chunk, exc)
                stats["failed"] += 1
            continue

        # 写入成功后再逐块回报；单块 409（token 已失效）不影响其余块
        for chunk in batch:
            try:
                await _complete_chunk(client, chunk, provider)
                stats["done"] += 1
            except Exception as exc:  # noqa: BLE001 - token 失效 / api 抖动
                # 【不要在这里回报 fail】向量已经写进 Qdrant 了 —— 这里失败的是
                # 「完成回执」而不是 embedding。回报 fail 会错误累加 attempts，
                # 甚至把一条其实已向量化的块打成 failed。留给超时回收后重新
                # complete（upsert 幂等），或下一轮重新认领再写一次。
                logger.warning("回报 chunk %s 完成失败（下轮/回收后重试）：%s", chunk["id"], exc)
                stats["complete_failed"] += 1

    # ---------- 撤回：delete + retracted ----------
    if retractions:
        ids = [item["id"] for item in retractions]
        try:
            await qdrant.delete(
                collection_name=settings.qdrant_collection,
                points_selector=models.PointIdsList(points=ids),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001 - 删除失败就不回报，下一轮自然重试
            logger.warning("撤回 %d 个向量失败（下轮重试）：%s", len(ids), exc)
        else:
            for item in retractions:
                try:
                    response = await client.post(f"/api/v1/chunks/{item['id']}/retracted")
                    response.raise_for_status()
                    stats["retracted"] += 1
                except Exception as exc:  # noqa: BLE001 - 没回报，下一轮会再删一次（幂等）
                    logger.warning("回报 chunk %s 撤回失败：%s", item["id"], exc)

    return stats


async def main() -> None:
    """
    worker 主循环：确保集合 → 建 provider → 轮询 ``index_once``。

    单轮异常只记录并 sleep，不退出进程；没有任务时按 ``IDLE_SLEEP`` 空转。
    """
    await ensure_collection()
    provider = get_provider(settings.embedding_provider)
    qdrant = get_qdrant()

    logger.info(
        "向量化 worker 已启动 -> %s（model=%s, dim=%s, collection=%s, batch=%d）",
        API_BASE,
        provider.model,
        provider.dim,
        settings.qdrant_collection,
        EMBED_BATCH_SIZE,
    )

    async with httpx.AsyncClient(base_url=API_BASE, timeout=30.0) as client:
        while True:
            try:
                stats = await index_once(client, qdrant, provider)
                if not any(stats.values()):
                    await asyncio.sleep(IDLE_SLEEP)
                else:
                    logger.info(
                        "本轮：认领 %d / 完成 %d / 失败 %d / 完成回报失败 %d / 撤回 %d",
                        stats["claimed"],
                        stats["done"],
                        stats["failed"],
                        stats["complete_failed"],
                        stats["retracted"],
                    )
            except Exception:  # noqa: BLE001 - 常驻进程不能因单轮异常退出
                logger.exception("本轮失败，%s 秒后重试", IDLE_SLEEP)
                await asyncio.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    asyncio.run(main())
