# RAG 评测报告（2026-11-03） - hybrid

## 环境

- 语料：<待填>（active chunk 数 / 文档数由主会话补充，JSON 的 ``corpus`` 字段同）
- embedding：BAAI/bge-small-zh-v1.5
- LLM：extractive（provider：extractive）
- prompt 版本：unknown
- git commit：fbcffd0
- 服务地址：http://localhost:8001
- 配置：hybrid（retrieval_mode=hybrid，top_k=5，candidate_k=50，use_cache=False，score_threshold=None）
- 题集：split=dev，n=35，每题 warm-up 1 次 + 正式 2 次取中位数，并发 1
- 时间：2026-11-03T01:13:49+08:00

## 结果（dev 集，n=35）

| 指标 | 值 |
|---|---:|
| Recall@5 | 100.0% |
| Evidence Recall@5 | 98.4% |
| MRR（top-5） | 93.2% |
| nDCG@10 | 94.1% |
| 引用准确率 | 39.2%（有引用的 32 题，覆盖率 100.0%） |
| 引用召回率 | 98.4% |
| 拒答准确率 | 0.0%（负样本 3 题） |
| 误拒率 | 0.0%（正样本 32 题） |
| 降级率 | 0.0% |

### 延迟（毫秒）

| 指标 | p50 | p95 |
|---|---:|---:|
| 检索 | 32 | 383 |
| 首 token | 38 | 416 |
| 总延迟 | 44 | 419 |

## 失败样例（自动摘取）

- **q-neg-001**（负样本未拒答，negative）：FastAPI 如何配置 Kubernetes Operator 来实现自动扩缩容？
  - gold：（无）
  - 检索：https://fastapi.tiangolo.com/zh/editor-support/, https://fastapi.tiangolo.com/zh/how-to/custom-request-and-route/, https://fastapi.tiangolo.com/zh/advanced/path-operation-advanced-configuration/, https://fastapi.tiangolo.com/zh/reference/templating/, https://fastapi.tiangolo.com/zh/
  - 回答：模型暂不可用，以下是检索到的相关内容：  [1] 标题：编辑器支持 来源：web 内容：编辑器支持¶ 官方的 FastAPI 扩展为你的 FastAPI 开发流程带来增强,包括路径操作的发现与导航、部署到 FastAPI Cloud,以及实时日志流式传输。 有关该扩展的更多详情,请参阅其 GitHub 仓库中的 RE…
- **q-neg-002**（负样本未拒答，negative）：FastAPI 内置的 GraphQL 订阅应该怎么实现？
  - gold：（无）
  - 检索：https://fastapi.tiangolo.com/zh/advanced/templates/, https://fastapi.tiangolo.com/zh/, https://fastapi.tiangolo.com/zh/how-to/graphql/, https://fastapi.tiangolo.com/zh/tutorial/first-steps/, https://fastapi.tiangolo.com/zh/how-to/graphql/
  - 回答：模型暂不可用，以下是检索到的相关内容：  [1] 标题：模板 来源：web 内容：Files,该 CSS 文件会由你的 FastAPI 应用在 URL /static/styles.css 自动提供。 更多说明¶ 包括如何测试模板在内的更多详情,请查看 Starlette 的模板文档。  from  fastapi …
- **q-neg-005**（负样本未拒答，negative）：FastAPI 如何配置 Celery 来执行定时任务？
  - gold：（无）
  - 检索：https://fastapi.tiangolo.com/zh/advanced/opentelemetry/, https://fastapi.tiangolo.com/zh/advanced/opentelemetry/, https://fastapi.tiangolo.com/zh/how-to/custom-request-and-route/, https://fastapi.tiangolo.com/zh/, https://fastapi.tiangolo.com/zh/advanced/advanced-dependencies/
  - 回答：模型暂不可用，以下是检索到的相关内容：  [1] 标题：OpenTelemetry 来源：web 内容：耗时和活跃请求数的指标。 自定义遥测¶ 配置 provider 和 exporter¶ provider 提供用于记录追踪、指标或日志的对象。其配置决定了这些数据的处理和导出方式。 遥测库可以配置 OpenTele…

## 口径说明

- 指标按 evidence URL 匹配；URL 去 fragment、去尾斜杠、host 小写后比较。
- 检索指标只在可回答且带 evidence 的题上统计；MRR 在配置的 top_k=5 内计算。
- 每题先 warm-up 1 次（不计分），再正式跑 runs 次：延迟取每题的中位数，再对题求 p50 / p95；指标内容取最后一次运行（缓存关闭，结果稳定）。
- nDCG@10 的二值相关性按 URL 去重；若 top_k < 10，实际只有 top_k 条参与。
- 引用准确率 / 召回率在「可回答且确实给出引用」的题上取平均；空引用题不计入平均，但计入引用覆盖率。
- 拒答判定：答案含「根据现有资料无法确定」，或 done.reason ∈ {no_context, low_score} 且 citations 为空。
- 不统计答案正确率：本地小模型 / extractive 兜底不足以自动判分。
- 缓存已关闭（use_cache=false）；若 JSON 里 cache_hit_runs > 0，说明配置没生效。

## 结论

- <待主会话对照 dense / hybrid 后填写>
