"""
核对 MySQL（真相）与 Qdrant（索引）的 chunk 集合是否一致。

用法（容器内，工作目录 ``/app``）：

    python -m scripts.check_consistency

比较口径：

- MySQL 侧： **未作废** 且 **已用当前模型完成向量化** 的 chunk id；
- Qdrant 侧：集合里 ``embedding_model`` 等于当前模型的 point id。

「当前模型」通常就是 ``EMBEDDING_MODEL``；只有 ``hash`` 桩例外（它的 ``model`` 固定是
``hash`` ，见 ``HashProvider``）。这里不复用 provider 实例，免得在没装 fastembed 的
ingest 容器里引爆依赖。

``missing_in_qdrant`` 表示「库里说已经向量化，但索引里找不到」——这是真丢数据；
``extra_in_qdrant`` 表示「索引里有、库里已作废或不存在」——通常是撤回任务还没跑完的
短暂窗口，也可能是残留脏点。

还有缺失或多余就以 ``1`` 退出（可直接接进巡检脚本）；集合不存在 / 连接失败以 ``2`` 退出。
"""
import asyncio
import sys

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.infra.qdrant_client import qdrant_client
from app.models.document_chunk import DocumentChunk, EmbeddingStatus

# 打印样例 id 的上限：排查时看几个就够，全量倒出去反而难读
_SAMPLE_SIZE = 20

# Qdrant scroll 每页条数
_PAGE_SIZE = 1000


def _current_model() -> str:
    """
    写入 payload 的 embedding 模型名。

    ``HashProvider.model`` 固定为 ``hash`` （测试桩），其余 provider 用 ``EMBEDDING_MODEL`` 。

    :return: 当前模型名。
    """
    return "hash" if settings.embedding_provider == "hash" else settings.embedding_model


async def _mysql_expected_ids() -> tuple[set[int], int]:
    """
    取「应当存在于 Qdrant」的 chunk id。

    只算 **已 done 且模型等于当前配置** 的块：pending / processing / failed 本来就不该在
    索引里，旧模型的向量也不在当前模型的检索口径内 —— 把它们算成 missing 会让工具在
    正常重嵌入期间一直报错。

    :return: ``(期望 id 集合, 未就绪（不参与比较）的 active 块数)``。
    """
    model = _current_model()
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    DocumentChunk.id,
                    DocumentChunk.embedding_status,
                    DocumentChunk.embedding_model,
                ).where(DocumentChunk.superseded_at.is_(None))
            )
        ).all()

    expected = {
        int(row.id)
        for row in rows
        if row.embedding_status == EmbeddingStatus.DONE
        and row.embedding_model == model
    }
    return expected, len(rows) - len(expected)


async def _qdrant_ids() -> set[int]:
    """
    分页 scroll 出集合里当前模型的全部 point id。

    scroll 用 ``offset`` 游标翻页，直到返回的 next offset 为 ``None`` 。

    :return: point id 集合。
    """
    model = _current_model()
    ids: set[int] = set()
    offset = None
    while True:
        points, offset = await qdrant_client.scroll(
            collection_name=settings.qdrant_collection,
            limit=_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            if payload.get("embedding_model") != model:
                continue
            ids.add(int(point.id))
        if offset is None:
            break
    return ids


def _print_samples(label: str, ids: set[int]) -> None:
    """
    打印样例 id（最多 ``_SAMPLE_SIZE`` 个）。

    :param label: 输出前缀。
    :param ids: 待打印的 id 集合。
    """
    sample = sorted(ids)[:_SAMPLE_SIZE]
    print(f"{label}（最多 {_SAMPLE_SIZE} 个）：{sample}")


async def _run() -> int:
    """
    执行核对并输出报告。

    :return: 进程退出码 —— ``0`` 一致，``1`` 有缺失 / 多余，``2`` 环境不可用。
    """
    try:
        if not await qdrant_client.collection_exists(settings.qdrant_collection):
            print(
                f"[check_consistency] Qdrant 集合 {settings.qdrant_collection!r} 不存在；"
                f"检查 QDRANT_URL / QDRANT_COLLECTION 是否指向了正确环境",
                file=sys.stderr,
            )
            return 2

        mysql_ids, not_ready = await _mysql_expected_ids()
        qdrant_ids = await _qdrant_ids()
    finally:
        # 脚本进程一次性的：用完就释放，别等解释器退出时再收尾
        await engine.dispose()
        await qdrant_client.close()

    missing = mysql_ids - qdrant_ids
    extra = qdrant_ids - mysql_ids

    print(f"collection            : {settings.qdrant_collection}")
    print(f"embedding_model       : {_current_model()}")
    print(f"mysql_expected        : {len(mysql_ids)}")
    if not_ready:
        print(f"mysql_not_ready       : {not_ready}（pending / failed / 旧模型，不参与比较）")
    print(f"qdrant_points         : {len(qdrant_ids)}")
    print(f"missing_in_qdrant     : {len(missing)}")
    print(f"extra_in_qdrant       : {len(extra)}")

    if missing:
        _print_samples("missing 样例 id", missing)
    if extra:
        _print_samples("extra 样例 id", extra)

    if missing or extra:
        print(
            "[check_consistency] 不一致：missing 需要重新向量化（scripts.reindex / embedder），"
            "extra 通常是撤回任务未跑完；若长期存在请检查 embedder 与 retracted 回报"
        )
        return 1

    print("[check_consistency] 一致")
    return 0


def main() -> None:
    """
    命令行入口：执行核对并以对应退出码结束。

    连接 / 查询异常统一打印成可读的一行，退出码 ``2`` —— 与「不一致」（``1``）区分开，
    巡检脚本能判断该修数据还是该看服务。
    """
    try:
        code = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - 脚本入口：打印清楚再以非零码退出
        print(f"[check_consistency] 执行失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    main()
