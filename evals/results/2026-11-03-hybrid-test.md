# RAG 评测报告（2026-11-03） - hybrid

## 环境

- 语料：<待填>（active chunk 数 / 文档数由主会话补充，JSON 的 ``corpus`` 字段同）
- embedding：BAAI/bge-small-zh-v1.5
- LLM：extractive（provider：extractive）
- prompt 版本：unknown
- git commit：fbcffd0
- 服务地址：http://localhost:8001
- 配置：hybrid（retrieval_mode=hybrid，top_k=5，candidate_k=50，use_cache=False，score_threshold=None）
- 题集：split=test，n=15，每题 warm-up 1 次 + 正式 2 次取中位数，并发 1
- 时间：2026-11-03T01:14:04+08:00

## 结果（test 集，n=15）

| 指标 | 值 |
|---|---:|
| Recall@5 | 76.9% |
| Evidence Recall@5 | 73.1% |
| MRR（top-5） | 73.1% |
| nDCG@10 | 71.3% |
| 引用准确率 | 42.9%（有引用的 13 题，覆盖率 100.0%） |
| 引用召回率 | 73.1% |
| 拒答准确率 | 0.0%（负样本 2 题） |
| 误拒率 | 0.0%（正样本 13 题） |
| 降级率 | 0.0% |

### 延迟（毫秒）

| 指标 | p50 | p95 |
|---|---:|---:|
| 检索 | 34 | 348 |
| 首 token | 38 | 433 |
| 总延迟 | 44 | 440 |

## 失败样例（自动摘取）

- **q-neg-003**（负样本未拒答，negative）：FastAPI 如何对接 Kafka 做消息队列削峰？
  - gold：（无）
  - 检索：https://fastapi.tiangolo.com/zh/advanced/websockets/, https://fastapi.tiangolo.com/zh/tutorial/header-params/, https://fastapi.tiangolo.com/zh/tutorial/header-param-models/, https://fastapi.tiangolo.com/zh/advanced/templates/, https://fastapi.tiangolo.com/zh/tutorial/dependencies/global-dependencies/
  - 回答：模型暂不可用，以下是检索到的相关内容：  [1] 标题：WebSockets 来源：web 内容：。 等待消息并发送消息¶ 在您的 WebSocket 路由中,您可以使用 await 等待消息并发送消息。 from fastapi import FastAPI, WebSocket from fastapi.resp…
- **q-neg-004**（负样本未拒答，negative）：FastAPI 的 gRPC 服务怎么定义 proto 文件？
  - gold：（无）
  - 检索：https://fastapi.tiangolo.com/zh/tutorial/background-tasks/, https://fastapi.tiangolo.com/zh/tutorial/request-files/, https://fastapi.tiangolo.com/zh/tutorial/frontend/, https://fastapi.tiangolo.com/zh/tutorial/handling-errors/, https://fastapi.tiangolo.com/zh/advanced/templates/
  - 回答：模型暂不可用，以下是检索到的相关内容：  [1] 标题：后台任务 来源：web 内容：后台任务¶ 你可以定义在返回响应后运行的后台任务。 这对需要在请求之后执行的操作很有用,但客户端不必在接收响应之前等待操作完成。 包括这些例子: - 执行操作后发送的电子邮件通知: - 由于连接到电子邮件服务器并发送电子邮件往往很“…
- **q-g2-020**（检索未命中，detail）：在 FastAPI 中怎样用 status 模块表示 403 禁止访问状态码？该常量的数值是多少？
  - gold：https://fastapi.tiangolo.com/zh/reference/status/
  - 检索：https://fastapi.tiangolo.com/zh/tutorial/response-status-code/, https://fastapi.tiangolo.com/zh/tutorial/response-status-code/, https://fastapi.tiangolo.com/zh/advanced/security/http-basic-auth/, https://fastapi.tiangolo.com/zh/advanced/additional-status-codes/, https://fastapi.tiangolo.com/zh/advanced/response-change-status-code/
  - 回答：模型暂不可用，以下是检索到的相关内容：  [1] 标题：响应状态码 来源：web 内容：响应状态码¶ 与指定响应模型的方式相同,在以下任意路径操作中,可以使用 status_code 参数声明用于响应的 HTTP 状态码: - @app.get() - @app.post() - @app.put() - @app.…
- **q-g2-021**（检索未命中，detail）：在普通 def 路径操作函数里，如何访问 UploadFile 参数底层的标准 Python 文件对象？
  - gold：https://fastapi.tiangolo.com/zh/reference/uploadfile/
  - 检索：https://fastapi.tiangolo.com/zh/advanced/using-request-directly/, https://fastapi.tiangolo.com/zh/tutorial/request-files/, https://fastapi.tiangolo.com/zh/tutorial/request-files/, https://fastapi.tiangolo.com/zh/tutorial/request-files/, https://fastapi.tiangolo.com/zh/tutorial/query-params/
  - 回答：模型暂不可用，以下是检索到的相关内容：  [1] 标题：直接使用 Request 来源：web 内容：直接使用 Request¶ 至此,我们已经使用多种类型声明了请求的各种组件。 并从以下对象中提取数据: - 路径参数 - 请求头 - Cookies - 等 FastAPI 使用这种方式验证数据、转换数据,并自动生成…
- **q-g2-022**（检索未命中，detail）：Depends() 的 use_cache 参数默认值是什么？把它设为 False 会怎样？
  - gold：https://fastapi.tiangolo.com/zh/reference/dependencies/
  - 检索：https://fastapi.tiangolo.com/zh/advanced/advanced-dependencies/, https://fastapi.tiangolo.com/zh/advanced/advanced-dependencies/, https://fastapi.tiangolo.com/zh/tutorial/query-param-models/, https://fastapi.tiangolo.com/zh/tutorial/security/simple-oauth2/, https://fastapi.tiangolo.com/zh/tutorial/query-params-str-validations/
  - 回答：模型暂不可用，以下是检索到的相关内容：  [1] 标题：高级依赖项 来源：web 内容：情况下,FastAPI 会使用这个 __call__ 来检查附加参数和子依赖,并且稍后会调用它,把返回值传递给你的路径操作函数中的参数。 参数化实例¶ 现在,我们可以用 __init__ 声明实例的参数,用来“参数化”这个依赖项:…

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
