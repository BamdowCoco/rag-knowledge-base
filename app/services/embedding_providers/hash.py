"""
``hash`` embedding provider： **无模型、可离线** 的词法哈希向量。

它不懂语义，但也不是随机数：把文本切成 **英文单词 + 中文/字符 2–3 gram** ，
每个 token 用 BLAKE2b 哈希到 ``dim`` 个桶里（带正负号），再做 L2 归一化。
于是共享关键词 / 字词的文本会有更高的余弦相似度 —— 在拿不到 embedding 模型的
离线环境（或测试）里，检索至少是"词法相关"的，而不是随机命中。

两条测试最需要的好性质仍然成立：

- **同一文本永远同向量** —— 重跑、重启、多进程都一致；
- **不同文本通常不同向量** —— 哈希桶碰撞会有少量噪声，但不影响确定性。

【为什么不直接用 ``random.Random(text)``】Python 的 ``str`` 哈希带进程随机盐，
内置 ``hash`` 也不能跨进程稳定；用 BLAKE2b 派生才是真确定性的。
"""
import hashlib
import math
import re

from app.core.config import settings

from .base import EmbeddingError

# 英文 / 数字 token：技术文档里的 ``Depends`` / ``HTTPException`` / ``status_code`` 靠它
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> list[str]:
    """
    把文本切成用于词法哈希的 token。

    英文按单词；中文（以及其它非空白字符）按 2–3 gram 切 —— 中文没有空格，
    字符 n-gram 是最省事的"伪分词"，能覆盖"依赖注入"这类词的大部分共享片段。

    :param text: 原文。
    :return: token 列表；空文本返回空列表。
    """
    lowered = text.lower()
    tokens = _WORD_RE.findall(lowered)
    compact = re.sub(r"\s+", "", lowered)
    for size in (2, 3):
        tokens.extend(compact[i:i + size] for i in range(len(compact) - size + 1))
    return tokens


def _derive_vector(text: str, dim: int) -> list[float]:
    """
    由文本派生 ``dim`` 维确定性词法向量，并做 L2 归一化。

    :param text: 原文；按 UTF-8 编码后参与哈希。
    :param dim: 目标维度。
    :return: 长度为 ``dim`` 的单位向量；没有可用 token 时返回零向量。
    """
    values = [0.0] * dim
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        index = raw % dim
        # 用另一 bit 决定符号：减少"所有 token 都往正方向堆"造成的偏差
        sign = 1.0 if (raw >> 8) & 1 else -1.0
        values[index] += sign

    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0.0:  # 空文本 / 纯空白 / 极短标点：没有可归一化的方向
        return [0.0] * dim
    return [value / norm for value in values]


class HashProvider:
    """
    确定性词法哈希向量（无模型、不出网、不依赖 onnxruntime）。
    """

    name = "hash"

    def __init__(self) -> None:
        """
        维度在实例化时从 ``settings`` 取 —— 测试改配置后 ``reset_provider_cache()``
        重建实例就能生效，不会沿用 import 时的旧值。
        """
        self.model = "hash"
        self.dim = settings.embedding_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        逐条派生确定性向量。

        :param texts: 文本列表；空列表返回 ``[]``。
        :return: 与 ``texts`` 同序的向量列表，每个长度均为 ``self.dim``。
        :raises EmbeddingError: 派生结果维度与 ``self.dim`` 不符（理论上不会发生）。
        """
        if not texts:
            return []
        vectors = [_derive_vector(text, self.dim) for text in texts]
        for vector in vectors:
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"hash provider 产出的维度是 {len(vector)}，期望 {self.dim}"
                )
        return vectors
