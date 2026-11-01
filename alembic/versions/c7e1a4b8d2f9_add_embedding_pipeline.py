"""add embedding pipeline columns: claim token / attempts / backoff / model version

给 ``document_chunk`` 补上「真实下游」要用的七列：

 - ``claim_token``        认领令牌，完成 / 失败时校验（fencing）
 - ``embedding_attempts`` 失败次数，到上限转 ``failed``（毒丸）
 - ``next_attempt_at``    失败退避后的最早认领时间
 - ``embedding_error``    最近一次失败原因
 - ``embedding_model``    产生该向量的模型（建索引，供 reindex 按模型扫）
 - ``embedding_dim``      向量维度（与 collection 校验）
 - ``embedded_at``        向量写入成功时刻

【为什么不在这里重洗数据】迁移只负责加列：把老 chunk 自动重置成 pending 会在
上线时悄悄触发全量重嵌入。重嵌入是显式运维动作（``scripts/reindex.py``），
本地 demo 也可以直接 ``docker compose down -v`` 重建。

Revision ID: c7e1a4b8d2f9
Revises: 9b1c4a7d2e30
Create Date: 2026-10-31
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c7e1a4b8d2f9"
down_revision: str | None = "9b1c4a7d2e30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_chunk",
        sa.Column("claim_token", sa.String(length=36), nullable=True),
    )
    # NOT NULL 的新列落在非空表上必须带 server_default 回填老行。
    # 这里保留 server_default="0"（与模型一致），也让多行 INSERT 省略该列时由库兜底。
    op.add_column(
        "document_chunk",
        sa.Column(
            "embedding_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )

    op.add_column(
        "document_chunk",
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "document_chunk",
        sa.Column("embedding_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "document_chunk",
        sa.Column("embedding_model", sa.String(length=120), nullable=True),
    )
    op.create_index(
        op.f("ix_document_chunk_embedding_model"),
        "document_chunk",
        ["embedding_model"],
        unique=False,
    )
    op.add_column(
        "document_chunk",
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
    )
    op.add_column(
        "document_chunk",
        sa.Column("embedded_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    # 反序删列 / 索引 —— 与 upgrade 严格对称
    op.drop_column("document_chunk", "embedded_at")
    op.drop_column("document_chunk", "embedding_dim")
    op.drop_index(op.f("ix_document_chunk_embedding_model"), table_name="document_chunk")
    op.drop_column("document_chunk", "embedding_model")
    op.drop_column("document_chunk", "embedding_error")
    op.drop_column("document_chunk", "next_attempt_at")
    op.drop_column("document_chunk", "embedding_attempts")
    op.drop_column("document_chunk", "claim_token")
