"""
爬虫配置：环境变量 → 冻结的 ``CrawlConfig`` 。

**为什么不用 ``pydantic-settings`` ：** crawler 是独立镜像、独立进程，只读十来个小字段，
没必要为它再背一套依赖；``dataclasses`` + ``os.getenv`` 足够，且字段默认值一眼能看全。

**默认值口径：** 全部与 ``docs/提升方案/02-真实上游-爬虫同步.md`` 及 compose 的 ``crawler``
服务一致。目标站实测不支持条件请求（没有 ``ETag`` / ``Last-Modified`` ），所以默认 sitemap
用的是中文站点的 ``/zh/sitemap.xml`` ，而不是主 sitemap。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# ``true`` 与 ``false`` 的常见写法。空字符串按 False 处理（compose 里 ``${VAR:-false}`` 不会给空值，
# 但手工 ``-e VAR=`` 会；把它当"关"比启动报错更符合直觉）。
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n", "f", ""})


def _env_bool(name: str, default: bool) -> bool:
    """
    读取布尔环境变量。

    :param name: 环境变量名。
    :param default: 未设置时用的默认值。
    :return: 解析后的布尔值。
    :raises ValueError: 值既不是真也不是假（拼错时尽早失败，别静默当成默认值）。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"环境变量 {name}={raw!r} 不是合法布尔值（可用 true/false、1/0、yes/no）")


def _env_int(name: str, default: int) -> int:
    """
    读取整数环境变量。

    :param name: 环境变量名。
    :param default: 未设置或为空时用的默认值。
    :return: 解析后的整数。
    :raises ValueError: 值不是整数。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    """
    读取浮点环境变量。

    :param name: 环境变量名。
    :param default: 未设置或为空时用的默认值。
    :return: 解析后的浮点数。
    :raises ValueError: 值不是数字。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)


@dataclass(frozen=True)
class CrawlConfig:
    """
    一轮爬取需要的全部参数。

    **冻结（ ``frozen=True`` ）：** 配置在进程内只读，避免某轮处理里被意外改写；
    需要变体（例如测试里改 ``bulk_size`` ）用 ``dataclasses.replace`` 派生新实例。
    """

    # ---------- 目标 ----------
    sitemap_url: str = "https://fastapi.tiangolo.com/zh/sitemap.xml"
    url_prefix: str = "https://fastapi.tiangolo.com/zh/"
    max_pages: int = 150
    interval_seconds: int = 21600
    miss_limit: int = 2
    replay: bool = False

    # ---------- 礼貌 ----------
    concurrency: int = 2
    delay_seconds: float = 1.0
    timeout_seconds: float = 15.0
    max_retries: int = 3
    user_agent: str = "rag-ingest-demo/0.1 (+https://github.com/BamdowCoco/rag-ingest-pipeline)"
    once: bool = True
    max_page_bytes: int = 2 * 1024 * 1024
    max_sitemap_bytes: int = 10 * 1024 * 1024
    min_content_chars: int = 100

    # ---------- 接入 ----------
    api_base: str = "http://api:8000"
    bulk_size: int = 30

    # ---------- Redis ----------
    redis_host: str = "redis"
    redis_port: int = 6379
    redis_db: int = 0

    @classmethod
    def from_env(cls) -> "CrawlConfig":
        """
        从环境变量构造配置（缺省项用 dataclass 默认值）。

        :return: 一份冻结的配置实例。
        """
        return cls(
            sitemap_url=os.getenv(
                "CRAWL_SITEMAP_URL", "https://fastapi.tiangolo.com/zh/sitemap.xml"
            ),
            url_prefix=os.getenv("CRAWL_URL_PREFIX", "https://fastapi.tiangolo.com/zh/"),
            max_pages=_env_int("CRAWL_MAX_PAGES", 150),
            interval_seconds=_env_int("CRAWL_INTERVAL_SECONDS", 21600),
            miss_limit=_env_int("CRAWL_MISS_LIMIT", 2),
            replay=_env_bool("CRAWL_REPLAY", False),
            concurrency=_env_int("CRAWL_CONCURRENCY", 2),
            delay_seconds=_env_float("CRAWL_DELAY_SECONDS", 1.0),
            timeout_seconds=_env_float("CRAWL_TIMEOUT_SECONDS", 15.0),
            max_retries=_env_int("CRAWL_MAX_RETRIES", 3),
            user_agent=os.getenv(
                "CRAWL_USER_AGENT",
                "rag-ingest-demo/0.1 (+https://github.com/BamdowCoco/rag-ingest-pipeline)",
            ),
            once=_env_bool("CRAWL_ONCE", True),
            max_page_bytes=_env_int("CRAWL_MAX_PAGE_BYTES", 2 * 1024 * 1024),
            max_sitemap_bytes=_env_int("CRAWL_MAX_SITEMAP_BYTES", 10 * 1024 * 1024),
            min_content_chars=_env_int("CRAWL_MIN_CONTENT_CHARS", 100),
            api_base=os.getenv("API_BASE", "http://api:8000"),
            bulk_size=_env_int("BULK_SIZE", 30),
            redis_host=os.getenv("REDIS_HOST", "redis"),
            redis_port=_env_int("REDIS_PORT", 6379),
            redis_db=_env_int("REDIS_DB", 0),
        )
