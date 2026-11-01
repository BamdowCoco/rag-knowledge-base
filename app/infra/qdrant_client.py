"""
Qdrant 向量库客户端（``AsyncQdrantClient``）。

照 ``redis_client.py`` / ``model_client.py`` 的形状： **模块级单例 + lifespan 关停时释放** 。
``AsyncQdrantClient`` 构造时 **不建立连接** ，首次调用才连 —— 所以 import 它不会拖慢启动。

【集合与命名向量】集合里只有一个命名向量 ``dense`` （``EMBEDDING_DIM`` 维、Cosine）。
用命名向量而不是默认向量，是为了将来加 ``sparse`` 做混合检索时不必再改集合结构。

【维度校验】``ensure_collection()`` 在集合已存在时会核对维度；不一致直接抛错 ——
Qdrant 集合的维度不能原地改，换模型 / 换维度要 **新建集合** （配置项 ``QDRANT_COLLECTION``）。

【payload 索引】检索时的 ``source`` / ``language`` / ``document_id`` 过滤，
以及按 ``embedding_model`` 找"旧模型产生的向量"（重嵌入），都靠这些索引；
建索引失败（通常是已存在 / 版本差异）只告警，不阻断启动。
"""
import logging

from qdrant_client import AsyncQdrantClient, models

from app.core.config import settings

logger = logging.getLogger(__name__)

# 命名向量的名字：写入、检索两侧都从这里取，别各写一份字面量
DENSE_VECTOR_NAME = "dense"

# 要建 payload 索引的字段 -> 索引类型
_PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "source": models.PayloadSchemaType.KEYWORD,
    "language": models.PayloadSchemaType.KEYWORD,
    "document_id": models.PayloadSchemaType.INTEGER,
    "embedding_model": models.PayloadSchemaType.KEYWORD,
}

# 模块级单例：整个进程共用一个客户端（内部维护连接池）。
# prefer_grpc=False：走 HTTP（6333），排查时 curl 也能看，少一层 gRPC 依赖。
qdrant_client: AsyncQdrantClient = AsyncQdrantClient(
    url=settings.qdrant_url,
    api_key=settings.qdrant_api_key or None,
    timeout=settings.qdrant_timeout_seconds,
    prefer_grpc=False,
    # 客户端 1.20 与服务端 1.12.4 的小版本差超过 1，默认会打兼容性警告；
    # 本项目只用 collection/upsert/query_points/scroll 这些稳定接口，显式关掉。
    check_compatibility=False,
)


def get_qdrant() -> AsyncQdrantClient:
    """
    给调用方拿这个共享客户端用。

    :return: 模块级单例 ``AsyncQdrantClient``。
    """
    return qdrant_client


async def ensure_collection() -> None:
    """
    确保集合存在且维度 / payload 索引符合配置（幂等，可每次启动都调）。

    - 集合不存在时按 ``EMBEDDING_DIM`` 建 ``dense`` 命名向量（Cosine）；
    - 集合已存在时校验 ``dense`` 的维度与配置一致，不一致抛 ``RuntimeError``；
    - payload 索引逐个创建，单个失败只告警。

    多 worker 同时启动时创建会撞车：若别的进程刚好建好，复核存在后继续，不当成致命错误。

    :raises RuntimeError: 集合存在但维度与 ``EMBEDDING_DIM`` 不一致，或缺少 ``dense`` 向量。
    """
    exists = await qdrant_client.collection_exists(settings.qdrant_collection)
    if not exists:
        try:
            await qdrant_client.create_collection(
                collection_name=settings.qdrant_collection,
                vectors_config={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=settings.embedding_dim,
                        distance=models.Distance.COSINE,
                    )
                },
            )
            logger.info(
                "已创建 Qdrant 集合 %s（%s 维，Cosine）",
                settings.qdrant_collection,
                settings.embedding_dim,
            )
        except Exception:
            # 竞态：另一个 worker 抢先建好了 —— 复核存在即可继续；真的没建起来才往上抛
            if not await qdrant_client.collection_exists(settings.qdrant_collection):
                raise
            logger.info("Qdrant 集合 %s 已由其它进程创建", settings.qdrant_collection)

    await _check_dimension()
    await _ensure_payload_indexes()


async def _check_dimension() -> None:
    """
    校验已存在集合的 ``dense`` 向量维度与 ``EMBEDDING_DIM`` 一致。

    :raises RuntimeError: 缺少 ``dense`` 向量或维度不一致。
    """
    info = await qdrant_client.get_collection(settings.qdrant_collection)
    vectors = info.config.params.vectors
    # 命名向量时 vectors 是 dict；兼容旧版本直接返回 VectorParams 的形状
    dense = vectors.get(DENSE_VECTOR_NAME) if isinstance(vectors, dict) else None
    if dense is None:
        raise RuntimeError(
            f"Qdrant 集合 {settings.qdrant_collection!r} 没有命名向量 {DENSE_VECTOR_NAME!r}；"
            f"请使用本服务创建的集合，或修改 QDRANT_COLLECTION"
        )
    if dense.size != settings.embedding_dim:
        raise RuntimeError(
            f"Qdrant 集合 {settings.qdrant_collection!r} 的向量维度是 {dense.size}，"
            f"与 EMBEDDING_DIM={settings.embedding_dim} 不一致。"
            f"换 embedding 模型 / 维度请新建集合并修改 QDRANT_COLLECTION（维度不能原地改）"
        )


async def _ensure_payload_indexes() -> None:
    """
    逐个创建 payload 索引；失败只告警（已存在、版本差异都不该挡住启动）。
    """
    for field_name, field_schema in _PAYLOAD_INDEXES.items():
        try:
            await qdrant_client.create_payload_index(
                collection_name=settings.qdrant_collection,
                field_name=field_name,
                field_schema=field_schema,
            )
        except Exception as exc:  # noqa: BLE001 - 索引问题不致命，记录后继续
            logger.warning(
                "Qdrant payload 索引 %s 创建失败（可忽略：%s）", field_name, exc
            )


async def close_qdrant() -> None:
    """
    关停时释放 Qdrant 客户端的连接（由 lifespan 调用）。
    """
    await qdrant_client.close()
