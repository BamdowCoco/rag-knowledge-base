"""add fulltext ngram index on document_chunk.content

给 ``document_chunk.content`` 建 FULLTEXT 索引，供 hybrid 检索的关键词一路使用。

**为什么必须带 ``WITH PARSER ngram``** ：MySQL 默认的全文解析器按空格切词，
中文整句会变成一个超长 token，几乎无法命中；ngram 解析器按 n 个字符滑窗切分
（``ngram_token_size`` 默认 2），「依赖注入」才能被切成「依赖 / 赖注 / 注入」参与匹配。

**代价**：这是一条 ``ALTER TABLE`` ，在 20 万行的 ``document_chunk`` 上重建索引需要
一段时间；生产库由 api 启动时的 ``alembic upgrade head`` 执行，期间写入会等待。
测试库不走 Alembic（``create_all`` 建表），同一条 ALTER 由 ``tests/conftest.py`` 补。

Revision ID: 5f3a9c2e7b14
Revises: c7e1a4b8d2f9
Create Date: 2026-11-03
"""
from collections.abc import Sequence

from alembic import op


revision: str = "5f3a9c2e7b14"
down_revision: str | None = "c7e1a4b8d2f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为 ``document_chunk.content`` 建 ngram FULLTEXT 索引。"""
    op.execute(
        "ALTER TABLE document_chunk "
        "ADD FULLTEXT INDEX ft_chunk_content (content) WITH PARSER ngram"
    )


def downgrade() -> None:
    """删除 FULLTEXT 索引，回到 dense-only 的 schema。"""
    op.execute("DROP INDEX ft_chunk_content ON document_chunk")
