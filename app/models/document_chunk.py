"""分块表：文档切成的 chunk，是下游向量化的【任务单元】。"""
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class EmbeddingStatus(StrEnum):
    """
    chunk 的向量化状态机：pending → processing → done。

    失败是一条侧路：未达上限时退回 pending（带退避），达上限转 failed 终态 ——
    否则确定性错误（比如模型维度配错）会让同一批块无限重试，队列永远不动。

    用 StrEnum 而不是裸字符串：状态名写错会立刻报错，
    而不是悄悄往库里存一个谁也不认识的字符串。
    """

    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class DocumentChunk(Base):
    __tablename__ = "document_chunk"
    # 同一文档内 chunk_index 唯一 —— 多一道防「重复切块」的保险
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_index"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 指向 document_clean.id —— 站在消费方视角，这就是「这篇文档」
    document_id: Mapped[int] = mapped_column(
        ForeignKey("document_clean.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(nullable=False)      # 0 起，同文档内有序
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(default=0, nullable=False)
    # 阶段 C 的认领查询按它筛，所以现在就加索引
    embedding_status: Mapped[str] = mapped_column(
        String(16), default=EmbeddingStatus.PENDING, index=True, nullable=False
    )
    # ---------- 认领 / 失败契约（P0：真实下游） ----------
    # 认领令牌：认领时写入 uuid，完成 / 失败时校验。
    # 这是 fencing —— 慢 worker 在超时回收、任务被别人重领之后才回来，
    # 旧 token 已经对不上，于是它写不进结果，防止「旧结果盖新结果」。
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 失败次数；到 ``max_embed_attempts`` 就转 failed，不再被认领（毒丸）。
    embedding_attempts: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), nullable=False
    )
    # 失败退避后的最早认领时间；NULL = 没失败过、立即可认领。
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 最近一次失败原因（截断到 2000 字符），便于排查。
    embedding_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 产生这条向量的模型 / 维度：换模型时据此找需要重嵌入的行，也用于检索侧过滤。
    # embedding_model 加索引 —— reindex 脚本按「模型不等于目标」扫 active 块。
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    embedding_dim: Mapped[int | None] = mapped_column(nullable=True)
    # 向量真正写进向量库并确认成功的时刻（不是认领时刻）。
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ---------- 更新 / 删除语义（阶段 F）----------
    # 非 NULL = 所属版本已作废。认领与回收都过滤掉它 —— 不给已作废的块白花 embedding 钱。
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 非 NULL = 下游已确认把该向量从向量库删掉（撤回任务的完成标记）
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
