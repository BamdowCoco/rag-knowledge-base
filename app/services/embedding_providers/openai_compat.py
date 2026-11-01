"""
OpenAI 兼容协议的 embedding 适配器：``POST {EMBEDDING_API_BASE_URL}{EMBEDDING_API_PATH}``。

覆盖自建服务（TEI / vLLM 的 ``/v1/embeddings``）与云厂商兼容端点。
和 ``enrich_providers/openai_compat.py`` 一个思路： **厂商差异关在这一个文件里** 。

【为什么不按返回顺序直接用】协议里的 ``data[]`` 带 ``index`` ，但 **不保证按序返回**
（并行推理的服务尤其容易乱序）。这里按 ``index`` 重排后再对齐 ``texts`` ——
顺序错了不会报错，只会把向量写到错误的 chunk 上。

【配置错误 vs 上游故障】``EMBEDDING_API_BASE_URL`` 为空是部署配置问题，
抛 ``EmbeddingError`` 让启动 / 任务显式失败；网络与 HTTP 状态错误由 ``raise_for_status`` 抛出。
"""
from typing import Any

from app.core.config import settings
from app.infra.ai_client import get_embedding_client

from .base import EmbeddingError


class OpenAICompatEmbeddingProvider:
    """
    走 OpenAI 兼容 ``/v1/embeddings`` 协议的 HTTP 上游。
    """

    name = "openai_compat"

    def __init__(self) -> None:
        """
        ``model`` / ``dim`` 在实例化时取配置：测试改配置后 ``reset_provider_cache()``
        重建实例即可生效。
        """
        self.model = settings.embedding_model
        self.dim = settings.embedding_dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        调上游批量向量化并按 ``index`` 还原顺序。

        :param texts: 文本列表；空列表返回 ``[]``（不打上游）。
        :return: 与 ``texts`` 同序的向量列表，每个长度等于 ``self.dim``。
        :raises EmbeddingError: 基址没配、返回条数不对、维度不对或缺少 ``embedding``。
        :raises httpx.HTTPStatusError: 上游返回非 2xx。
        """
        if not texts:
            return []

        if not settings.embedding_api_base_url:
            raise EmbeddingError(
                "EMBEDDING_PROVIDER=openai_compat 但 EMBEDDING_API_BASE_URL 为空"
            )

        # 两侧都归一，免得用户把基址写成带尾斜杠 / 路径写成不带前斜杠
        url = (
            f"{settings.embedding_api_base_url.rstrip('/')}/"
            f"{settings.embedding_api_path.lstrip('/')}"
        )
        payload: dict[str, Any] = {"model": self.model, "input": texts}
        # 自建服务通常不要 key；云厂商兼容端点才需要 —— 所以 key 是可选的
        headers = (
            {"Authorization": f"Bearer {settings.embedding_api_key}"}
            if settings.embedding_api_key
            else None
        )

        response = await get_embedding_client().post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

        items = data.get("data") or []
        if len(items) != len(texts):
            raise EmbeddingError(
                f"embedding 上游返回 {len(items)} 条向量，输入是 {len(texts)} 条"
            )

        # 按 index 重排（index 缺失时退回它在数组里的位置），确保与 texts 一一对应
        ordered = sorted(
            enumerate(items), key=lambda pair: pair[1].get("index", pair[0])
        )
        vectors: list[list[float]] = []
        for _, item in ordered:
            embedding = item.get("embedding")
            if not isinstance(embedding, list):
                raise EmbeddingError("embedding 上游返回的 data[] 缺少 embedding 数组")
            if len(embedding) != self.dim:
                raise EmbeddingError(
                    f"embedding 上游向量维度是 {len(embedding)}，期望 {self.dim}"
                    f"（检查 EMBEDDING_MODEL 与 EMBEDDING_DIM 是否匹配）"
                )
            vectors.append([float(value) for value in embedding])
        return vectors
