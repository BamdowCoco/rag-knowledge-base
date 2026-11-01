"""
embedding 适配层的公共底座：协议 + 错误类型。

三个 provider（``hash`` / ``fastembed`` / ``openai_compat``）出参形状一致：
``list[list[float]]``，且 **顺序必须与输入 ``texts`` 一一对应** 。
调用方靠位置把向量写回对应 chunk —— 顺序错了不会报错，
只会把 A 的向量写到 B 上（最难查的那类 bug），所以这条要求写在协议里。
"""
from typing import Protocol


class EmbeddingError(Exception):
    """
    embedding 失败：配置错误（端点没配）、上游不可用、返回数量 / 维度不对等。

    与"没有向量"不同 —— 它表示 **这一批没算成** ，调用方应把对应 chunk 记为失败，
    交给退避重试或转 ``failed`` 终态。
    """


class EmbeddingProvider(Protocol):
    """
    一个 embedding provider 的形状。

    - ``name`` 是注册表里的名字；``model`` / ``dim`` 是写入 Qdrant 与 chunk 行时要记的元数据；
    - ``embed`` **返回顺序必须与 ``texts`` 一致** ；
    - **失败必须抛** （``EmbeddingError`` 或底层异常）：不能返回空列表或短列表假装成功。
    """

    name: str
    model: str
    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        把一批文本编码成向量。

        :param texts: 文本列表；空列表应直接返回 ``[]``（不必打上游）。
        :return: 与 ``texts`` 等长、同序的向量列表，每个向量长度等于 ``dim``。
        :raises EmbeddingError: 配置错误、上游失败或返回数量 / 维度不匹配。
        """
        ...
