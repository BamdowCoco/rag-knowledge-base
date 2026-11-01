"""
``fastembed`` embedding provider：本地 ONNX 推理（CPU），默认档。

【懒加载是硬要求】 ``fastembed`` / ``onnxruntime`` 只装在 ``rag-api`` 与 ``embedder`` 镜像里；
ingest API 与 test 镜像用 ``hash`` 桩，装不下这两坨依赖。所以 **本模块顶层不 import fastembed** ，
只有注册表在选中 ``fastembed`` 时才会 import 本文件、再在 ``__init__`` 里 import 它。

【模型只加载一次】 ``TextEmbedding(...)`` 要读权重、建 ONNX 会话，慢到秒级；
provider 在注册表里是单例，所以这件事每个进程只发生一次 —— 别在每个请求里 new。

【同步转异步】fastembed 的 ``embed`` 是同步阻塞的，直接调用会卡住事件循环；
用 ``asyncio.to_thread`` 丢到线程池，避免拖慢同进程的其它请求。
"""
import asyncio

from app.core.config import settings

from .base import EmbeddingError


class FastEmbedProvider:
    """
    本地 ONNX embedding（BAAI/bge 系列等）。
    """

    name = "fastembed"

    def __init__(self) -> None:
        """
        加载 ONNX 模型（每进程一次，耗时秒级）。

        ``model`` / ``dim`` 在实例化时取配置：测试改配置后 ``reset_provider_cache()``
        重建实例即可生效。

        :raises ImportError: 当前镜像没装 ``fastembed``（应改用 ``hash`` 或 ``openai_compat``）。
        """
        # 故意放在函数里：保证 import 注册表 / 本模块不会连带 import onnxruntime。
        # 这里捕获 ImportError 是为了给"在宿主机上直接跑"的人一句能看懂的提示：
        # fastembed 只装在 rag-api / embedder 镜像（requirements-rag.txt），
        # ingest API 与 test 镜像不装它。
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 取决于运行镜像
            raise ImportError(
                "当前环境没有安装 fastembed。fastembed 只装在 rag-api / embedder 镜像里；"
                "请用 requirements-rag.txt 构建镜像，或改用 EMBEDDING_PROVIDER=hash。"
            ) from exc

        self.model = settings.embedding_model
        self.dim = settings.embedding_dim
        # 离线/内网：如果已经准备好 ONNX 模型目录，直接用它，不再联网下载
        extra: dict[str, str] = {}
        if settings.embedding_model_path:
            extra["specific_model_path"] = settings.embedding_model_path
        self._model = TextEmbedding(
            model_name=self.model,
            cache_dir=settings.embedding_cache_dir,
            **extra,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """
        批量编码；fastembed 返回 numpy 数组，这里转成纯 ``list`` 以便 JSON / Qdrant 序列化。

        :param texts: 文本列表；空列表返回 ``[]``（不触发模型推理）。
        :return: 与 ``texts`` 同序的向量列表，每个长度等于 ``self.dim``。
        :raises EmbeddingError: 返回条数或维度与 ``texts`` / ``self.dim`` 不符。
        """
        if not texts:
            return []

        # 模型推理是 CPU 密集的同步调用 —— 丢线程池，别阻塞事件循环
        vectors = await asyncio.to_thread(
            lambda: [list(map(float, vector)) for vector in self._model.embed(texts)]
        )

        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"fastembed 返回 {len(vectors)} 条向量，输入是 {len(texts)} 条"
            )
        for vector in vectors:
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"fastembed 向量维度是 {len(vector)}，期望 {self.dim}"
                    f"（检查 EMBEDDING_MODEL 与 EMBEDDING_DIM 是否匹配）"
                )
        return vectors
