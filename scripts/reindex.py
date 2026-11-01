"""
把 active chunk 重置为 ``pending``，让 embedder 用新模型重新向量化。

用法（容器内，工作目录 ``/app``）：

    python -m scripts.reindex                     # 只重置「模型不是当前 EMBEDDING_MODEL」的块
    python -m scripts.reindex --model BAAI/bge-m3 # 指定目标模型名
    python -m scripts.reindex --all               # 重置所有 active 块（换集合 / 全量重建）
    python -m scripts.reindex --dry-run           # 只统计，不写库

**不物理删除 Qdrant 里的点。** point id 就是 ``chunk_id`` ，重新写入走 upsert，
同一个 id 会被覆盖；换 embedding 模型 / 维度时请先建新集合、再切
``QDRANT_COLLECTION`` ，然后 ``--all`` 重嵌入 —— 集合维度不能原地改。
"""
import argparse
import asyncio
import sys

from sqlalchemy import func, or_, select, update

from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models.document_chunk import DocumentChunk, EmbeddingStatus
from app.utils.time import utcnow


async def reset_chunks(model: str, *, reset_all: bool, dry_run: bool) -> int:
    """
    把目标块重置为待向量化，返回影响条数。

    目标是 **未作废** 且「从未向量化 / 向量来自别的模型」的块；``reset_all`` 时只按
    «未作废» 过滤。重置会清空失败计数、退避与旧的模型元数据 —— 这些都与旧向量绑定，
    留着会让新模型重试时被旧状态误导。

    :param model: 目标 embedding 模型（写入 ``embedding_model`` 的预期值）。
    :param reset_all: 是否忽略模型版本、重置所有 active 块。
    :param dry_run: 只统计命中条数，不写库。
    :return: 命中（或实际重置）的条数。
    """
    conditions = [DocumentChunk.superseded_at.is_(None)]
    if not reset_all:
        conditions.append(
            or_(
                DocumentChunk.embedding_model.is_(None),
                DocumentChunk.embedding_model != model,
            )
        )

    async with AsyncSessionLocal() as db:
        if dry_run:
            count_stmt = (
                select(func.count()).select_from(DocumentChunk).where(*conditions)
            )
            return int((await db.execute(count_stmt)).scalar_one())

        stmt = (
            update(DocumentChunk)
            .where(*conditions)
            .values(
                embedding_status=EmbeddingStatus.PENDING,
                embedding_attempts=0,
                claim_token=None,
                next_attempt_at=None,
                embedding_error=None,
                embedding_model=None,
                embedding_dim=None,
                embedded_at=None,
                updated_at=utcnow(),
            )
        )
        result = await db.execute(stmt)
        await db.commit()
        return int(result.rowcount or 0)


async def _run(model: str, *, reset_all: bool, dry_run: bool) -> int:
    """
    跑重置并释放连接池（脚本进程用完即退，别把连接挂到进程结束）。

    :param model: 目标 embedding 模型。
    :param reset_all: 是否重置所有 active 块。
    :param dry_run: 是否只统计。
    :return: 命中（或实际重置）的条数。
    """
    try:
        return await reset_chunks(model, reset_all=reset_all, dry_run=dry_run)
    finally:
        await engine.dispose()


def main() -> None:
    """
    命令行入口：解析参数 → 执行 → 打印结果。

    失败时以非零码退出，方便脚本 / CI 判断。
    """
    parser = argparse.ArgumentParser(
        description="把需要重嵌入的 active chunk 重置为 pending"
    )
    parser.add_argument(
        "--model",
        default=settings.embedding_model,
        help="目标 embedding 模型名（默认取 EMBEDDING_MODEL）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="reset_all",
        help="重置所有 active chunk（默认只重置模型不等于目标模型的）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将影响多少条，不写库",
    )
    args = parser.parse_args()

    try:
        count = asyncio.run(_run(args.model, reset_all=args.reset_all, dry_run=args.dry_run))
    except Exception as exc:  # noqa: BLE001 - 脚本入口：打印清楚再以非零码退出
        print(f"[reindex] 失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if args.dry_run:
        print(f"[reindex] 将重置 {count} 条（目标模型：{args.model}，dry-run 未写库）")
    else:
        print(f"[reindex] 已重置 {count} 条为 pending（目标模型：{args.model}）")
        print("[reindex] embedder 下一轮会重新认领并写入 Qdrant")


if __name__ == "__main__":
    main()
