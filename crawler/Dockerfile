# 真实上游爬虫的独立镜像：只装 httpx / redis / 正文抽取，不背项目主镜像的 DB / 向量依赖。
FROM python:3.13-slim

# 国内构建加速：与根 Dockerfile 一致，需要官方源时用 --build-arg 覆盖。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# PYTHONUNBUFFERED：日志不缓冲，docker logs 才能实时看到
# PYTHONDONTWRITEBYTECODE：不在镜像里生成 __pycache__
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先只拷依赖清单并安装：requirements 不变时改代码不会重装依赖
COPY requirements.txt ./crawler/requirements.txt
RUN pip install --no-cache-dir -r crawler/requirements.txt

# 再拷代码到 /app/crawler（构建上下文就是 crawler/ 目录），保持包结构给 `python -m crawler.main`
COPY . ./crawler/

CMD ["python", "-m", "crawler.main"]
