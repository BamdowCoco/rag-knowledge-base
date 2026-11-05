# 企业知识库 RAG 数据接入与预处理管道

> 多个同步器（飞书 / Confluence / 语雀 / 本地上传）把文档推进来，管道负责
> **标准化 → 内容去重 → 落库 → 切块 → 派发向量化任务**，
> 为下游的向量检索 / RAG 提供干净、切好块、带状态的数据。

**去重挡掉重复同步 —— 省的是按量计费的 embedding 调用。**

---

## 要解决的问题

企业知识库的数据是这么进来的：

- **多源**：同一份文档往往同时挂在飞书和 Confluence 上，两个同步器都会推过来；
- **反复同步**：同步器按周期跑，**文档没变也会再推一遍**；
- **体量大**：一次全量同步可能来几千篇，每篇要切块、每块要向量化。

不做处理，下游就会收到大量重复文档，**把同一份内容反复切块、反复调 embedding**。

本服务就是接入层：接收 → 标准化 → 内容去重 → 落库 → 切块 → 标记待向量化；
对外提供文档 / 分块查询，以及给下游 worker 的**任务认领**接口。
**两个核心指标**：去重率（挡掉多少重复同步）、向量化积压（还有多少 chunk 没处理）。

![数据流：写入路（标准化 → 判身份 → 过准入 → 落库 → 切块 → 标 pending）、消费路（embedder 认领 → 向量库）、撤回路（版本作废 → retractions 交付）三路对照](assets/diagrams/02-dataflow.png)

---

## 快速开始

> 只在 `docker compose` 里跑，**宿主不需要装 MySQL / Redis / Python**；前提是 Docker Desktop 已在运行。
> 首次 `--build` 要装依赖 + 预热 tiktoken 词表与 jieba 词典，慢几分钟，之后就快。
> 命令都给了两版（Windows 在前），**两种 shell 里一样的就只写一行**。

### 1. 准备配置

```text
# Windows（cmd）
copy .env.example .env

# PowerShell / Git Bash / WSL / macOS
cp .env.example .env
```

### 2. 起服务

```bash
docker compose up -d --build
docker compose ps          # 期望：api / mysql / redis 是 healthy，其余 Up
```

常驻容器：`api`（本服务）、`rag-api`（检索问答，8001）、`mysql` / `redis` / `qdrant`、
`syncer`（模拟上游）、`embedder`（模拟下游 worker）、`model`（假模型服务，enrich 的替身上游）。
`crawler`（真实上游爬虫）只在显式 `--profile crawler` 时运行，见下面第 7 步。

![架构总览：docker compose 里的六个服务、网络 backend、两个命名卷与端口映射](assets/diagrams/01-architecture.png)

### 3. 发一篇文档

样例 JSON 就在仓库里（`scripts/example-doc.json`）。**走文件、别把 JSON 内联进命令行** ——
中文不过命令行就不会被转码（Windows 上这两个都踩过坑）。

```text
# Windows（cmd / PowerShell）—— 就一行，注意是 curl.exe
curl.exe -s -i -X POST http://localhost:8000/api/v1/documents -H "Content-Type: application/json" --data-binary "@scripts/example-doc.json"
# 期望：最上面一行是 HTTP/1.1 201

# Git Bash / WSL / macOS
curl -s -w '\n%{http_code}\n' -X POST http://localhost:8000/api/v1/documents \
  -H 'Content-Type: application/json' --data-binary @scripts/example-doc.json
# 期望：末尾一行是 201
```

### 4. 看流水线跑起来

```bash
docker compose logs -f syncer embedder
```

同步器在按周期推文档、worker 在并发认领 chunk —— 这就是"上游重推 → 去重挡住 → 下游按状态干活"的现场。

### 5. 查库

两个包装是一对，**都不必输口令**，也没有那句 "insecure" 警告：

```text
# Windows（cmd / PowerShell；PowerShell 里写 .\scripts\mysql.cmd）
scripts\mysql.cmd "select count(*) from document_clean;"

# Git Bash / WSL / macOS
bash scripts/mysql.sh "select count(*) from document_clean;"
```

### 6. 跑测试

```bash
docker compose --profile test run --rm test
```

> 更细的逐步验收（每一步"应该看到什么"）、常见症状、Windows 上的 shell 差异，
> 都在 `docs/README-旧版.md`（本地留存，未入库）。

### 7. 真实上游爬虫（可选）

`crawler` 从 FastAPI 中文文档的 sitemap 发现页面，走同一套 `/documents/bulk` 接入：
先读 `robots.txt`，单 host 并发 2、请求起始间隔 ≥1s；`ETag` / `Last-Modified` 加正文哈希做增量判断；
404/410 立即发删除信号，从 sitemap 连续两轮消失才删。

```text
# Windows（cmd / PowerShell）：跑一轮，抓 5 页
docker compose --profile crawler run --rm -e CRAWL_MAX_PAGES=5 crawler

# 重推同一批内容，演示接口去重（document_clean 行数不增加）
docker compose --profile crawler run --rm -e CRAWL_MAX_PAGES=5 -e CRAWL_REPLAY=true crawler

# 常驻：每 6 小时一轮
set CRAWL_ONCE=false && docker compose --profile crawler up -d crawler

# PowerShell
$env:CRAWL_ONCE="false"; docker compose --profile crawler up -d crawler

# Git Bash / WSL / macOS
CRAWL_ONCE=false docker compose --profile crawler up -d crawler
```

> 数据来源：`https://fastapi.tiangolo.com/zh/`（FastAPI 官方中文文档，MIT）。
> 只做本地检索演示，不重新分发抓到的全文。
> 默认 `CRAWL_ONCE=true`，`run --rm` 抓完一轮即退出；`CRAWL_REPLAY=true` 跳过缓存强制重推。
> 验收时按 `source='web'` 过滤：`scripts\mysql.cmd "select count(*) from document_clean where source='web';"`

### 8. 打开演示前端

```text
# 一条命令：准备 .env → 起 compose → 停 mock syncer → 可选抓 20 页 → 打印地址
scripts\demo.cmd

# PowerShell
.\scripts\demo.ps1
```

| 页面 | 地址 | 功能 |
|---|---|---|
| 聊天页 | `http://localhost:8001/` | 流式回答、引用 chips、来源/语言/top-k 筛选、反馈 |
| 运维页 | `http://localhost:8001/admin.html` | 文档数、来源、chunk 状态、去重、爬虫最近一轮统计、最近文档 |
| 关于页 | `http://localhost:8001/about.html` | 架构、技术栈、诚实边界 |
| API 文档 | `http://localhost:8000/docs` | 摄入侧接口 |

冒烟（会推一篇样例文档）：

```text
python scripts\smoke.py
```

> 前端由 `rag-api` 同源提供；运维页从 8001 调 api（8000）的统计接口，走白名单 CORS。
> 运维页只在本机使用，不要通过公网隧道暴露。
>
> `demo.cmd` / `demo.ps1` 启动后会**停掉 mock `syncer`**，让来源分布和“重复同步拦截”只反映真实
> `web` 语料；想看重复同步演示时执行 `docker compose start syncer` 恢复。
>
> 来源含义：`web` = P1 爬虫抓的 FastAPI 中文文档；`feishu` / `confluence` / `yuque` = mock 同步器样例；
> `bench` = 压测数据（运维页默认隐藏）。
>
> 当前 `LLM_PROVIDER=extractive` 是**检索片段模式**，不是故障；想接真实模型时启动 Ollama 并改 `.env`。
> 本分支只做本地 demo；VPS / Caddy / Cloudflare 隧道未做。

### 9. 接云模型（可选，无需本地大模型）

后端已经支持 OpenAI 兼容的 `/chat/completions`，**不需要在本机拉取大模型**。在 `.env` 里改
下面几项，然后重启 `rag-api`：

```env
LLM_PROVIDER=openai_compat
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-...
LLM_PATH=/chat/completions
```

示例端点（任选其一，按厂商文档确认模型名）：

| 厂商 | `LLM_BASE_URL` | `LLM_MODEL` 示例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| SiliconFlow | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` |
| 通义 / 其他兼容端点 | 厂商的 `/v1` 基址 | 厂商模型名 |

```text
docker compose up -d --no-deps rag-api
```

- 按 token 计费；`LLM_MAX_TOKENS` / `LLM_CONTEXT_TOKENS` 控制单次成本；
- 运维页「LLM 使用」用响应里的 `model` + 实际 `usage`（含 `prompt_cache_hit_tokens` /
  `prompt_cache_miss_tokens`），按 DeepSeek 中文官方页的人民币价（2026-11-05 抓取）和
  请求时的北京 peak/off-peak 时段自动估算费用；未知模型只显示 token，不估算；
- 聊天页会把模型回答按 **Markdown** 渲染（标题/列表/代码块/表格/链接），HTML 先经
  `DOMPurify` 白名单清洗；
- 每条回答底部显示总 tokens、prompt/completion 分项、首 token/总耗时/检索耗时；
  能匹配官方价格表时显示单次估算费用；
- **不要把 key 写进仓库或 README**，`.env` 已被 `.gitignore` 忽略；
- 云端失败时仍会自动降级到 extractive，`done.degraded=true`；
- 接上云模型后，回答由模型组织，不再是“原文片段列表”。

---

## 接口一览

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/v1/documents` | 提交单篇（`external_id` 必填）。新建 `201`；内容没变 `200`；同 id 改了内容 `200`；内容撞车 `409` |
| POST | `/api/v1/documents/bulk` | 批量提交（同步器的真实调用方式，一次 ≤200 篇） |
| POST | `/api/v1/documents/delete` | 删除信号（按 `source + external_id`，**幂等**，恒 `200`） |
| GET | `/api/v1/documents` | 分页查询（`page/limit/source/q`），走缓存 |
| GET | `/api/v1/documents/{id}` | 单篇详情 |
| GET | `/api/v1/documents/{id}/chunks` | 分块列表（分页） |
| GET | `/api/v1/documents/stats` | 文档数 / 按来源 top10 / 今日新增 / 重复同步拦截 / chunk 状态分布 |
| GET | `/api/v1/documents/crawler-stats` | 爬虫最近一轮统计（`crawl:stats` / `last_run` / `seen`） |
| POST | `/api/v1/chunks/claim` | 下游 worker **认领**待向量化的 chunk（`SKIP LOCKED`，可并发抢） |
| POST | `/api/v1/chunks/{id}/complete` | worker 上报完成 |
| POST | `/api/v1/search` | 向量 / hybrid 检索（rag-api，8001）；请求可带 `retrieval_mode`、`use_cache` |
| POST | `/api/v1/chat/stream` | 带引用的流式问答（SSE）；请求可带 `retrieval_mode`、`use_cache`、`score_threshold` |
| GET | `/metrics` | Prometheus 文本指标（rag-api / api，需 `APP_ROLE in (all, rag)`） |

> **检索模式**：默认 `dense`（纯向量）；`retrieval_mode="hybrid"` 会叠加 MySQL FULLTEXT
> ngram 关键词召回并用 RRF 融合。评测显示 hybrid 在 dev 有提升、test 没有，因此**默认不开启**，
> 见下面「评测」一节。

> **「重复」不是错误**：同步器周期性地跑，文档没变也会重推 —— 所以语义是"已存在，跳过"（`200`），
> 同步器不必把正常同步当异常处理。

![文档一生时序图：从同步器推入、Redis 快路径与 MySQL 落库，到下游 embedder 认领 / 回报的一块任务单元](assets/diagrams/03-lifecycle.png)

---

## 评测（P2）

`evals/golden.jsonl` 是 50 道中文 golden set：18 事实 / 9 操作 / 9 对比 / 9 细节 / 5 负样本，
按 70/30 切成 dev 35 / test 15。评测关闭缓存，按 evidence URL 计算 Recall@5、MRR、nDCG，
以及引用准确率 / 召回率、拒答准确率与检索 / 首 token / 总延迟。

test 集（13 道可回答 + 2 负样本）实测：

| 配置 | Recall@5 | MRR | nDCG@10 | 引用准确率 | 检索 p95 | 总 p95 |
|---|---:|---:|---:|---:|---:|---:|
| dense（默认） | 0.769 | 0.769 | 0.740 | 0.396 | 19 ms | 27 ms |
| hybrid | 0.769 | 0.731 | 0.713 | 0.430 | 348 ms | 440 ms |

**结论**：hybrid 在 dev 上提升明显（Recall@5 0.9375 → 1.0、MRR 0.757 → 0.932），但 test 上没有
召回提升、MRR/nDCG 略降、检索 p95 多约 330 ms。因此 **默认保留 dense**，hybrid 作为按需开关。
负样本拒答为 0：负样本与正样本的 `best_dense_score` 重叠，任何能拒掉负样本的阈值都会造成
>9% 误拒；下一步应改用 LLM 拒答提示或 cross-encoder，而不是继续调余弦阈值。

```text
# 从宿主机（需要 httpx + pyyaml）
python -m scripts.eval_rag --config dense  --base-url http://localhost:8001 --split test
python -m scripts.eval_rag --config hybrid --base-url http://localhost:8001 --split test

# 或用 eval 容器（结果经 ./evals 挂载回仓库）
docker compose --profile eval run --rm eval python -m scripts.eval_rag --config dense --split test
```

完整报告在 `evals/results/2026-11-03-summary.md`，原始 JSON 在同目录。

---

## 设计要点

### 1. 双层去重：Redis 快路径 + MySQL 唯一索引做权威

**做了什么**：提交时先问 Redis Set（挡掉绝大多数重复），真正入库时由
`document_dedup.content_hash` 的唯一索引兜底。

**为什么不能只留一层**：Redis 会被清空 / 淘汰 / 重启丢数据，**不能当真相**；
而每次都查库，在同步洪峰下又太贵。两层分工 —— 快路径省往返，唯一索引管正确性
（Redis 说"见过"而库里没有时会自愈）。

**为什么值得做两层**：去重挡掉的不只是数据库往返，更是下游**按量计费**的 embedding 调用。

### 2. 内容指纹 ≠ 文档身份

**做了什么**：指纹是 `sha256(标题 + 正文)`；"这是不是同一篇"由另一张表回答 ——
`document_source` 存 `(source, external_id)` 唯一，`document_clean` 是**版本表**
（`superseded_at` 标出当前版本），更新 / 删除时还要**通知下游撤回**哪些块（`retracted_at`）。

**为什么一个键不够**：指纹只回答"这份内容见过没"。改了标题就换了指纹，同一篇的更新拦不住；
反过来，两篇不同的文档也可能内容一样，会把新文档误判成重复。

![数据库 ER：document_source（身份）— document_clean（版本）— document_chunk（任务单元）三张表，加 document_raw（原始留痕）与 document_dedup（内容准入权威）](assets/diagrams/04-er.png)

### 3. 任务分发用 `SKIP LOCKED`，不是分布式锁；而且必须有超时回收

**做了什么**：`/chunks/claim` 用 `SELECT ... FOR UPDATE SKIP LOCKED` 认领一批 chunk 并置 `processing`。

**为什么不是锁**：锁回答的是"这件事只能一个人做"（保护临界区），
`SKIP LOCKED` 回答的是"这堆活大家分着做"（分发任务）—— 向量化是后者。

**为什么一定要回收**：worker 领完任务然后崩了，那条就永远卡在 `processing`、没人再碰它。
所以既要有回收循环（超时放回 `pending`），也要有"已作废的块不再认领"的过滤 ——
不给已经不要的数据白花 embedding 钱。

### 4. 切块按 token 精确控制，不是按字符

**做了什么**：目标块大小按 **token** 定（tiktoken 精确数），切法是递归字符分割（段落 → 句子 → 硬切）+ overlap。

**为什么不按字符**：块大小是由"模型能吃多少 token"决定的，而中文一个字常占 2 个 token ——
"每 500 字切一刀"会让每块的实际长度飘忽，块大小不可控。
overlap 则是为了跨块语义连贯：检索命中一块时，不至于因为切在句子中间而丢上下文。

### 5. AI 接缝可插拔，且 AI 不拦请求路径

**做了什么**：enrich（语言 / 摘要 / 关键词 / 署名）由**后台循环**补，落库时只标 `pending`；
上游可插拔，provider 一个文件一档（`rule` / `model` / `openai_compat` / `off`）。

**为什么不放在请求路径上**：模型是秒级的**外部依赖**，它慢了、挂了都不能让文档进不来。
降级链最终回落到纯 Python 的规则档，文档照常 `201`，那几列稍后补上（或保持规则算出来的值）。

**实测过**：`openai_compat` 这一档接过真的云厂商（DeepSeek），**只改配置、没改代码**。

---

## 技术栈

| 类别 | 选型 |
|---|---|
| Web 框架 | FastAPI · Pydantic v2 |
| 数据库 | MySQL 8（SQLAlchemy 2.0 Async + aiomysql） |
| 迁移 | Alembic（启动时 `upgrade head`，空库首启自动建表） |
| 缓存 / 去重 / 锁 | Redis 7（`redis.asyncio`）：去重快路径、Cache-Aside、分布式锁 |
| 并发原语 | `SKIP LOCKED` 任务分发 · Lua 比对 token 释放锁 · 版本号让缓存失效 |
| 容器 | Docker Compose：api · rag-api · mysql · redis · qdrant · syncer · embedder · model · crawler（profile） |
| 同步 / Worker | httpx（批量推送、认领任务、调模型服务）；`syncer` / `embedder` 可 `--scale` 多实例 |
| 真实上游 | sitemap 发现 · robots 校验 · 限速/退避 · ETag/Last-Modified + 正文哈希增量 · trafilatura 正文抽取 |
| 检索增强 | dense（默认）· MySQL FULLTEXT ngram + RRF 的 hybrid（按需开启，评测后决定不默认启用） |
| 可观测性 | prometheus-client：HTTP / 检索 / LLM / 缓存 / 积压指标，顶层 `/metrics` |
| AI 增强 | 规则档（py3langid + jieba）或 HTTP 上游（含 OpenAI 兼容协议） |
| 测试 | pytest + pytest-asyncio（真 MySQL + 真 Redis + 真 Qdrant，316 条） |

---

## 目录结构

```
rag-ingest-pipeline/
├── app/
│   ├── main.py                        # FastAPI 入口；lifespan 起停后台循环、释放连接
│   ├── api/
│   │   ├── deps.py                    # 依赖注入：db session / redis / service
│   │   └── v1/
│   │       ├── router.py              # 路由聚合（stats 必须先于 /{id} 注册）
│   │       └── endpoints/
│   │           ├── documents.py       #   提交 / 批量 / 删除 / 查询 / 分块列表
│   │           ├── chunks.py          #   认领 / 上报完成
│   │           └── stats.py           #   统计
│   ├── services/                      # 业务编排（不写 SQL、不碰 HTTP）
│   │   ├── document_service.py        #   标准化 → 加锁 → 判重 → 落库 → 切块 → 派发
│   │   ├── normalize_service.py       #   纯函数：标准化（去 HTML / 全角 / 零宽…）
│   │   ├── chunk_service.py           #   纯函数：切块（段落优先 + overlap）
│   │   ├── dedup_service.py           #   去重快路径（Redis）与计数
│   │   ├── lock_service.py            #   分布式锁（Lua 释放）
│   │   ├── cache_service.py           #   Cache-Aside + 版本号失效
│   │   ├── task_service.py            #   chunk 认领 / 完成（SKIP LOCKED）
│   │   ├── enrich_service.py          #   AI 接缝 facade：分派 provider + 降级链
│   │   ├── enrich_providers/          #   AI 适配层：一个 provider 一个文件
│   │   ├── enricher.py                #   后台补 enrich 的循环
│   │   └── reclaimer.py               #   超时回收循环（chunk + enrich）
│   ├── repositories/                  # 所有 DB 读写（含 SKIP LOCKED 认领）
│   ├── models/                        # ORM 表：raw / clean / dedup / source / chunk
│   ├── schemas/                       # Pydantic 出入参
│   ├── infra/                         # Redis 客户端 / 模型服务的 HTTP 客户端
│   ├── core/                          # 配置 / 日志 / 业务异常与统一映射
│   └── utils/                         # 无状态纯工具（hash / 时间 / 语言 / 关键词 / 切样）
├── syncer/                            # 模拟上游：周期性批量推文档
├── crawler/                           # 真实上游：sitemap 发现 + 条件请求 + 批量推送
├── embedder/                          # 模拟下游：认领 chunk 做（模拟）向量化
├── model/                             # 假模型服务（零依赖；enrich 的可插拔上游）
├── alembic/                           # 迁移（异步 engine，DSN 取自配置）
├── assets/diagrams/                   # README 用的图（PNG；可编辑源在本地 docs/diagrams/）
├── evals/                             # golden set、评测配置与实测结果
├── scripts/                           # 上游探活、查库包装、评测 runner、样例 JSON
├── tests/                             # pytest（真 MySQL + 真 Redis + 真 Qdrant）
└── docker-compose.yml                 # 服务编排 + 网络 + 卷
```

**分层与数据流**：`endpoint → service → repository → DB`；缓存 / 去重 / 锁走 Redis；
调模型服务只发生在**后台循环**里（不在请求路径上）；业务异常在 `core/exceptions.py` 统一映射成响应。
