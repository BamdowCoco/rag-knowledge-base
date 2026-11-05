# ---- 基础镜像（生产用的 runtime stage）----
FROM python:3.13-slim AS runtime

# 国内构建加速：pip 默认走 pypi.org，在部分网络下会卡住。
# 需要官方源时用 --build-arg PIP_INDEX_URL=https://pypi.org/simple/ 覆盖。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# PYTHONUNBUFFERED：日志不缓冲，docker logs 才能实时看到（不加会攒着一次性输出）
# PYTHONDONTWRITEBYTECODE：不在镜像里生成 __pycache__
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ---- 先只拷依赖清单并安装 ----
# 利用层缓存：只要 requirements.txt 没变，改代码时这一层直接命中缓存，不用重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
 && python -c "import jieba; jieba.initialize()"

# ---- 再拷代码 ----
# 这里只拷 app/（不拷 .env：镜像里不留口令，配置由 compose 的 env_file/environment 注入）
COPY app/ ./app/
# 演示前端（零构建静态页）：APP_ROLE=all / rag 时由 FastAPI 挂到 /；test stage 继承本 stage，
# 所以测试镜像里也能验证静态页与资源路径
COPY web/ ./web/

# 运维小工具（如 scripts/probe_upstream.py 单验上游）—— 拷进来就能 `docker compose exec` 直接跑，
# 不必额外挂卷（api 有 container_name，`compose run` 会和在跑的容器抢名字）
COPY scripts/ ./scripts/

# 迁移：alembic 的配置 + 迁移脚本。lifespan 里要跑 `upgrade head`，所以必须在镜像里
COPY alembic.ini .
COPY alembic/ ./alembic/

EXPOSE 8000

# 不加 --reload：容器内不做热重载。改完代码重建即可：docker compose up -d --build api
# --workers 默认 4，可用 API_WORKERS 覆盖；exec 保证 SIGTERM 能传到 uvicorn（优雅关停）。
# 每个 worker 会各起一份后台循环（enrich / reclaim），两者都按 SKIP LOCKED / 条件 UPDATE
# 实现，多进程安全。
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${API_WORKERS:-4}"]


# ---- RAG / embedder stage：真实 embedding + 问答 ----
# rag-api 与 embedder 都用它：比 runtime 多装 fastembed（onnxruntime 那套只在这里）。
# ingest api 与 test 仍从 runtime 起，不背这部分体积与模型依赖。
FROM runtime AS rag
COPY requirements-rag.txt .
ENV HF_ENDPOINT=https://hf-mirror.com \
    FASTEMBED_CACHE_PATH=/models/fastembed
# 构建期只装依赖，不下载模型：模型由 compose 从宿主机只读挂载到
# /models/bge-small-zh-v1.5（scripts/fetch_embedding_model 下载），provider 走
# specific_model_path 直接加载。这样构建不会被 HuggingFace 网络卡住。
RUN pip install --no-cache-dir -r requirements-rag.txt
COPY embedder/ ./embedder/


# ---- 测试 stage（多阶段构建的第二个 target）----
# 用法：docker compose --profile test run --rm test
# FROM runtime 而不是重开一个 slim：直接继承上面【构建期已预热】的 tiktoken 词表，
# 否则测试进程首次 import 又会去联网下词表。
FROM runtime AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

# pytest 的 asyncio 配置在 pyproject.toml 里（生产镜像不拷它，也就读不到那段配置）
COPY pyproject.toml .
# 爬虫（可选上游）也在测试范围内：先装它的运行依赖（层缓存），再拷代码
COPY crawler/requirements.txt ./crawler/requirements.txt
RUN pip install --no-cache-dir -r crawler/requirements.txt
COPY crawler/ ./crawler/
COPY tests/ ./tests/
# 评测包（指标纯函数 + YAML 配置）：test 镜像同时承载 compose 的 eval 服务
COPY evals/ ./evals/
# 测试会直接 import embedder.main 跑 index_once（用 hash provider，不需要 fastembed）
COPY embedder/ ./embedder/

CMD ["pytest", "-q"]
