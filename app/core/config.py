"""
应用配置（pydantic-settings）

思路：Settings 里只写「拆开的原子字段」(host/port/user…)，DSN 用 @property 现拼 ——
口令里的特殊字符要转义，而且不必在配置里再存一份拼好的串。

**环境只有一种口径：容器。** 这个项目只在 ``docker compose`` 里跑
（Windows 本地装不了 Redis，也没必要装），所以 host/port 一律是 compose 的服务名
（``mysql:3306`` / ``redis:6379``）—— 由 compose 的 ``environment:`` 指定，见 ``docker-compose.yml``。
"""
from typing import Literal
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",            # .env 只放账号 / 口令 / 业务参数；host/port 由 compose 的 environment 给
        env_file_encoding="utf-8",
        extra="ignore",             # .env 里有无关的键也不报错
        case_sensitive=False,       # 大小写忽略
    )

    # ---------- 应用 ----------
    app_name: str = "企业知识库 RAG 接入管道"
    debug: bool = False
    api_port: int = 8000
    log_level: str = "INFO"

    # ---------- MySQL ----------
    # 默认值就是容器口径（compose 的服务名）：缺环境变量时也不会连错地方
    db_host: str = "mysql"
    db_port: int = 3306
    db_user: str = "app"
    db_password: str = "app"
    db_name: str = "rag_ingest"
    # 每个 worker 的连接池。总连接数 = API_WORKERS × (size + overflow)；
    # 4 × (5 + 10) = 60，低于 MySQL 默认 max_connections=151。
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # ---------- Redis ----------
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0
    # 每进程连接池上限。应 ≥ 单进程最大并发；DB 池单进程上限是 10+20=30。
    # 写死 20 时压测并发 32 会直接抛 MaxConnectionsError（不排队）。
    redis_max_connections: int = 50

    # ---------- 业务参数 ----------
    cache_ttl: int = 60          # 列表/统计缓存秒数（阶段 7、8）
    lock_ttl_ms: int = 10000     # 分布式锁毫秒级 TTL（阶段 6）
    # bulk 入口每多少篇提交一次事务；1 = 逐篇提交（退回旧行为，也是回滚开关）
    bulk_commit_size: int = 10
    # 片内「身份唯一 + 内容唯一」时走多行 INSERT 快路径；false 退回逐篇切片
    bulk_multirow_enabled: bool = True
    # 单批允许的最大 chunk 行数（超过就退回逐篇，防大事务与包过大）
    bulk_max_chunk_rows: int = 2000

    # ---------- 后台任务 ----------
    reclaim_interval_seconds: int = 60    # 回收任务多久跑一次
    reclaim_after_seconds: int = 300      # processing 卡住超过多久算 worker 挂了

    # ---------- enrich（阶段 H）----------
    # provider：rule（纯 Python）/ model（自定契约的 HTTP 上游）/ openai_compat（OpenAI 兼容）/ off
    # 用 Literal 而不是裸 str —— 写错值启动就报错，不会静默变成"谁也不知道的模式"
    enrich_provider: Literal["rule", "model", "openai_compat", "off"] = "rule"
    enrich_model_url: str = "http://model:8000/enrich"   # `model` 档：上游地址（契约由我方定）
    # `openai_compat` 档：基址 + 端点路径 + 要哪个模型 + 可选的 key
    enrich_openai_base_url: str = "http://model:8000"
    # 端点路径做成可配：多数厂商是 `/v1/chat/completions`（DeepSeek / 通义 / Kimi / OpenAI 都是），
    # 但不是所有 —— 比如智谱 GLM 是 `/api/paas/v4/chat/completions`。写死一个就会挡掉一整家。
    enrich_openai_path: str = "/v1/chat/completions"
    enrich_model_name: str = "fake-model"
    # key 是可选的：自建推理（Ollama / vLLM）不需要，云厂商兼容端点才需要
    enrich_api_key: str = ""
    # 有些厂商支持 `response_format: {"type": "json_object"}`（DeepSeek / 通义 / GLM）——
    # 开了它模型更不容易跑偏。但不是所有兼容实现都支持，不支持会 400 → 整档不可用，所以默认关。
    enrich_json_mode: bool = False
    enrich_max_tokens: int = 512         # 摘要不该无限长；也是成本闸门
    enrich_sample_limit: int = 4000      # 发给上游前先截到这个长度（首段 + 尾段）
    enrich_timeout_seconds: float = 5.0
    # 后台补的节奏 —— 与 reclaim_* 并列，都是运维参数
    enrich_interval_seconds: int = 30
    enrich_batch_size: int = 50

    # ---------- 应用角色（P0：同一镜像按角色挂路由 / 起后台循环） ----------
    # all = 本地默认（全部路由 + 后台循环）；ingest = 只写（摄入 + 回收 / enrich）；
    # rag = 只读（检索 / 问答），不跑迁移、不起后台循环。
    app_role: Literal["all", "ingest", "rag"] = "all"

    # ---------- embedding（P0：真实下游） ----------
    # provider 决定向量从哪来：hash（测试桩，不出网）/ fastembed（本地 ONNX）/
    # openai_compat（OpenAI 兼容的向量端点）。用 Literal 让写错值在启动时就暴露。
    embedding_provider: Literal["hash", "fastembed", "openai_compat"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    # 维度必须与模型一致，也要与 Qdrant collection 的 size 对齐；换维度要新建集合。
    embedding_dim: int = 512
    embedding_batch_size: int = 32
    embedding_timeout_seconds: float = 10.0
    # fastembed 的 ONNX 模型缓存目录 —— 挂命名卷，避免每次重建镜像都重新下载。
    embedding_cache_dir: str = "/models/fastembed"
    # 可选：已经下载好的 ONNX 模型目录（离线/内网环境用）。
    # 填了它时 fastembed 不再联网下载，直接用这个目录里的模型文件。
    embedding_model_path: str = ""
    # 只有 openai_compat 档用得上：基址 + 端点路径 + 可选 key（自建服务通常不要 key）
    embedding_api_base_url: str = ""
    embedding_api_key: str = ""
    embedding_api_path: str = "/v1/embeddings"

    # ---------- Qdrant ----------
    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str = ""
    # 集合名带版本：换 embedding 模型 / 维度时新建集合再切配置，不做原地改维度。
    qdrant_collection: str = "document_chunks_v1"
    qdrant_timeout_seconds: float = 10.0

    # ---------- LLM（RAG 问答） ----------
    # openai_compat 覆盖 Ollama / vLLM / 云厂商兼容端点；extractive 是无模型兜底。
    llm_provider: Literal["openai_compat", "extractive"] = "openai_compat"
    llm_base_url: str = "http://host.docker.internal:11434/v1"
    llm_model: str = "qwen2.5:3b-instruct"
    llm_api_key: str = ""
    llm_path: str = "/chat/completions"
    llm_timeout_seconds: float = 60.0
    llm_max_tokens: int = 1024
    llm_temperature: float = 0.2
    # 拼 prompt 时可用的上下文预算（token）—— 超了按分数从低往高截掉片段。
    llm_context_tokens: int = 6000

    # ---------- 失败重试 / 毒丸 ----------
    # 同一个 chunk 连续失败到上限就转 failed 终态，避免确定性错误（比如维度配错）
    # 让队列永远在 pending / processing 之间打转。
    max_embed_attempts: int = 3
    # 指数退避的基数：第 n 次失败后等 backoff * 2**(n-1) 秒才可再次被认领。
    embed_backoff_seconds: int = 5

    # ---------- 检索 / 问答缓存 ----------
    qa_cache_enabled: bool = True
    qa_cache_ttl: int = 3600
    # prompt 模板版本：改了模板就 +1，让旧缓存 key 自然失效。
    prompt_version: int = 1
    retrieval_top_k: int = 5
    # 低于该分数的召回直接判为证据不足（0 = 不启用；阈值要用评测集校准，别拍脑袋）。
    retrieval_score_threshold: float = 0.0

    # ---------- 派生 DSN（用 @property 现拼，不重复存字段） ----------
    @property
    def database_url(self) -> str:
        """
        从拆开的字段拼出异步 MySQL DSN。

        :return: ``mysql+aiomysql://`` 形式的连接串（口令已做 URL 转义）。
        """
        # 密码里可能出现 @ : / 等字符，必须转义，否则 URL 会被解析错
        pwd = quote_plus(self.db_password)
        return (
            f"mysql+aiomysql://{self.db_user}:{pwd}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def redis_url(self) -> str:
        """
        从拆开的字段拼出 Redis DSN。

        :return: ``redis://host:port/db`` 形式的连接串。
        """
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


# 模块级单例：全局共享一份
settings = Settings()
