# 测试目录说明

> 测试按**改动区域**分包：只改一块时跑对应包，不用每次跑全量。
> 全量只在提交/发布前跑。

---

## 1. 包地图

| 包 | 覆盖什么 | 依赖 |
|---|---|---|
| `tests/unit/` | 纯函数：标准化、切块、token、embedding provider、extractive、fusion、评测指标、价格表 | 无 DB / Redis / Qdrant |
| `tests/ingest/` | 文档摄入、去重、身份/版本、更新/删除、bulk 写入、死锁重试 | MySQL + Redis + Qdrant |
| `tests/tasks/` | chunk 认领、`SKIP LOCKED`、超时回收、撤回 | MySQL + Redis + Qdrant |
| `tests/rag/` | Qdrant 索引、检索、hybrid、SSE 问答、QA 缓存 | MySQL + Redis + Qdrant |
| `tests/llm/` | LLM 用量统计（真实 usage / 费用） | MySQL + Redis + Qdrant |
| `tests/api/` | 统计、列表缓存、CORS、静态页、metrics、爬虫 stats | MySQL + Redis + Qdrant |
| `tests/enrich/` | enrich 规则、后台循环、provider | MySQL + Redis + Qdrant |
| `tests/infra/` | 分布式锁 | 只 Redis |
| `tests/crawler/` | sitemap / 抽取 / 条件请求 / 推送 / 状态 | 只 Redis（部分为纯函数） |

`tests/unit/conftest.py` 会把根 conftest 的 `prepare_database` / `prepare_qdrant` /
`_clean` 覆盖成 no-op；`tests/infra` 与 `tests/crawler` 只做 Redis 清理。
根 `tests/conftest.py` 仍是 DB/Qdrant 公共脚手架。

---

## 2. 常用命令

```text
# 纯函数：最快，不启动 MySQL/Qdrant
docker compose --profile test run --rm test pytest -q tests/unit

# 文档摄入 / 去重 / 版本 / bulk
docker compose --profile test run --rm test pytest -q tests/ingest

# chunk 任务分发 / 回收 / 撤回
docker compose --profile test run --rm test pytest -q tests/tasks

# 向量化 / 检索 / 问答 / QA 缓存
docker compose --profile test run --rm test pytest -q tests/rag

# LLM 用量
docker compose --profile test run --rm test pytest -q tests/llm

# API / 缓存 / 统计 / 静态页 / CORS / metrics
docker compose --profile test run --rm test pytest -q tests/api

# enrich
docker compose --profile test run --rm test pytest -q tests/enrich

# 锁（只 Redis）
docker compose --profile test run --rm test pytest -q tests/infra

# 爬虫（只 Redis）
docker compose --profile test run --rm test pytest -q tests/crawler

# 只收集不执行（改目录结构后先跑这个）
docker compose --profile test run --rm test pytest --collect-only -q tests

# 全量（提交/发布前）
docker compose --profile test run --rm test
```

> `test` 服务把 `./tests` 挂进容器，所以**只改测试文件**不必重建镜像；
> 如果改的是 `app/` 代码，先 `docker compose --profile test run --rm --build test`，
> 或按原流程重建 test 镜像。

---

## 3. 你改了什么 → 跑哪个包

| 改动区域 | 先跑 |
|---|---|
| 清洗 / 切块 / token / prompt / fusion / 价格表 | `tests/unit` |
| 文档提交、去重、身份/版本、bulk | `tests/ingest` |
| chunk 认领 / 回收 / 撤回 | `tests/tasks` |
| embedding / Qdrant / 检索 / SSE 问答 / QA 缓存 | `tests/rag` |
| LLM 用量 / 费用 | `tests/llm` + `tests/unit` |
| 统计 / 列表缓存 / CORS / 静态页 / metrics | `tests/api` |
| enrich 规则 / 后台循环 / provider | `tests/enrich` |
| 锁 | `tests/infra` |
| 爬虫 | `tests/crawler` |

跨包改动（例如同时改 `answer_service` 和 `llm_usage_service`）再叠加对应包；
拿不准时跑全量。
