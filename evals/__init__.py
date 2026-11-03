"""
RAG 评测包：题集、指标纯函数与运行配置。

模块划分：

- ``evals.metrics``：纯函数指标（召回 / 排序 / 引用 / 拒答 / 分位数），不依赖网络与数据库；
- ``evals.configs``：评测配置（``dense.yaml`` / ``hybrid.yaml``）；
- ``evals.results``：runner 产出的 JSON 与 Markdown 报告；
- ``evals.golden.jsonl``：题集，由主会话造题后写入。

runner 在 ``scripts/eval_rag.py``，语料导出工具在 ``scripts/export_eval_corpus.py``。
"""
