"""
从 MySQL 导出 active chunk，供造评测题使用（开发工具，不属于运行时链路）。

一行 = 一个 chunk（JSONL），字段：``document_id`` / ``url`` / ``title`` /
``chunk_index`` / ``content``。过滤条件：

- ``document_clean.source = --source``（默认 ``web``）；
- ``document_clean.superseded_at IS NULL`` 且 ``document_chunk.superseded_at IS NULL``
  —— 只看当前版本，作废版本不进题集。

**stdout 只写 JSONL**，进度日志一律走 stderr，这样重定向到文件得到的就是纯数据。

用法（容器内，工作目录 ``/app``）：

    docker compose exec api python -m scripts.export_eval_corpus --source web > web_chunks.jsonl
    docker compose exec api python -m scripts.export_eval_corpus --limit 200 --output /tmp/web_chunks.jsonl
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import TextIO

from sqlalchemy import select

from app.db.session import AsyncSessionLocal, engine
from app.models.document_chunk import DocumentChunk
from app.models.document_clean import DocumentClean


def build_statement(source: str, limit: int):
    """
    拼导出查询：clean ← chunk 内连接，只取两侧都未作废的行。

    :param source: 文档来源（对应 ``document_clean.source``）。
    :param limit: 最多导出多少个 chunk；``0`` 表示不限制。
    :return: SQLAlchemy ``select`` 语句。
    """
    statement = (
        select(
            DocumentClean.id,
            DocumentClean.url,
            DocumentClean.title,
            DocumentChunk.chunk_index,
            DocumentChunk.content,
        )
        .join(DocumentChunk, DocumentChunk.document_id == DocumentClean.id)
        .where(
            DocumentClean.source == source,
            DocumentClean.superseded_at.is_(None),
            DocumentChunk.superseded_at.is_(None),
        )
        .order_by(DocumentClean.id, DocumentChunk.chunk_index)
    )
    if limit > 0:
        statement = statement.limit(limit)
    return statement


async def export_chunks(source: str, limit: int, out: TextIO) -> int:
    """
    流式导出 chunk 到 ``out``，返回写出的行数。

    用 ``session.stream`` 逐行取，避免一次性把整站正文读进内存。

    :param source: 文档来源。
    :param limit: 最多导出多少个 chunk；``0`` 表示不限制。
    :param out: 输出流（文件或 stdout）。
    :return: 写出的行数。
    """
    count = 0
    try:
        async with AsyncSessionLocal() as session:
            result = await session.stream(build_statement(source, limit))
            async for row in result:
                payload = {
                    "document_id": row.id,
                    "url": row.url,
                    "title": row.title,
                    "chunk_index": row.chunk_index,
                    "content": row.content,
                }
                out.write(json.dumps(payload, ensure_ascii=False) + "\n")
                count += 1
                if count % 500 == 0:
                    print(f"[export] 已导出 {count} 个 chunk", file=sys.stderr)
    finally:
        # 脚本进程用完即退，别把连接池挂到进程结束
        await engine.dispose()
    return count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    解析命令行参数。

    :param argv: 参数列表；``None`` 表示取 ``sys.argv``。
    :return: 解析结果。
    """
    parser = argparse.ArgumentParser(description="导出 active chunk 的 JSONL（造题用）")
    parser.add_argument(
        "--source",
        default="web",
        help="文档来源过滤（默认 web）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="最多导出多少个 chunk（默认 0 = 不限制）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="输出文件路径（默认 stdout，只写 JSONL）",
    )
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit 不能为负数")
    return args


def main(argv: list[str] | None = None) -> None:
    """
    命令行入口：打开输出流 → 导出 → 关闭并打印统计。

    :param argv: 参数列表；``None`` 表示取 ``sys.argv``。
    """
    args = parse_args(argv)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle: TextIO = path.open("w", encoding="utf-8", newline="\n")
    else:
        # Windows 控制台默认可能是 GBK，显式转 UTF-8 避免中文正文写不出去
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        handle = sys.stdout

    try:
        count = asyncio.run(export_chunks(args.source, args.limit, handle))
    except Exception as exc:  # noqa: BLE001 - 脚本入口：打印清楚再以非零码退出
        print(f"[export] 失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    finally:
        if args.output:
            handle.close()

    print(f"[export] 已导出 {count} 个 chunk（source={args.source}）", file=sys.stderr)


if __name__ == "__main__":
    main()
