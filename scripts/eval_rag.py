"""
RAG 评测 runner：对 ``/api/v1/search`` 与 ``/api/v1/chat/stream`` 跑 golden set，
计算检索 / 引用 / 拒答 / 延迟指标，输出 JSON + Markdown。

用法（容器内，工作目录 ``/app``）：

    docker compose --profile eval run --rm eval python -m scripts.eval_rag --config dense --split test
    docker compose --profile eval run --rm eval python -m scripts.eval_rag --config hybrid --retrieval-only
    python -m scripts.eval_rag --config evals/configs/dense.yaml --base-url http://rag-api:8000

评测口径：

- 每题先 warm-up 一次（不计分），再跑 ``--runs`` 次；延迟取各次中位数；
- 指标内容取 **最后一次** 运行（关闭缓存后结果稳定，便于复现），缓存命中次数单独记录；
- 按 evidence URL 匹配（同一页面的多个 chunk 视为同一证据），URL 经
  ``evals.metrics.normalize_evidence_url`` 归一化；
- 负样本（``answerable=false``）只参与拒答指标，不参与检索 / 引用指标；
- **不统计答案正确率** —— 本地小模型 / extractive 兜底不足以自动判分，只做自动可测的指标；
- ``candidate_k`` 只作为配置口径记录，请求体只覆盖 ``retrieval_mode`` / ``use_cache`` /
  ``score_threshold``（候选池大小由服务端配置决定）。

退出码：全部成功为 0；golden 缺失、配置错误、HTTP 失败为 2。
"""
import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from evals.metrics import (
    citation_precision_recall,
    evidence_recall_at_k,
    mrr,
    ndcg_at_k,
    percentile,
    recall_at_k,
    refusal_metrics,
)

# runner 自己算指标用的固定 k：报告里写「Recall@5 / nDCG@10」就按这个口径
RECALL_K = 5
NDCG_K = 10

SEARCH_PATH = "/api/v1/search"
CHAT_PATH = "/api/v1/chat/stream"

# 拒答判定：答案文本命中固定话术，或 done.reason 属于「无上下文 / 低分」且没有引用
REFUSAL_MARKERS = ("根据现有资料无法确定",)
REFUSAL_REASONS = ("no_context", "low_score")

# 默认路径一律锚到仓库根（容器内是 /app），不依赖调用时的 cwd
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "evals" / "configs"
RESULTS_DIR = REPO_ROOT / "evals" / "results"
DEFAULT_GOLDEN = REPO_ROOT / "evals" / "golden.jsonl"

# 容器内默认打 rag-api 服务名；宿主机跑请用 --base-url http://localhost:8001 覆盖
DEFAULT_BASE_URL = os.environ.get("EVAL_BASE_URL", "http://rag-api:8000")


class EvalError(RuntimeError):
    """评测可预期失败（配置 / 题集 / HTTP），入口统一转成退出码 2。"""


# ---------- 配置与题集 ----------


def load_config(value: str) -> dict[str, Any]:
    """
    读评测配置：既接受短名（``dense``），也接受 YAML 路径。

    :param value: 配置短名或 ``.yaml`` 路径。
    :return: 配置字典（已做必填与取值校验）。
    :raises EvalError: 文件不存在、YAML 解析失败或字段非法。
    """
    looks_like_path = value.endswith((".yaml", ".yml")) or "/" in value or "\\" in value
    path = Path(value) if looks_like_path else CONFIG_DIR / f"{value}.yaml"
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise EvalError(f"配置文件不存在：{path}")

    try:
        with path.open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise EvalError(f"配置文件 YAML 解析失败：{path}：{exc}") from exc

    if not isinstance(config, dict):
        raise EvalError(f"配置文件顶层必须是映射：{path}")

    name = config.get("name")
    if not isinstance(name, str) or not name.strip():
        raise EvalError(f"配置缺少非空字符串字段 name：{path}")

    retrieval_mode = config.get("retrieval_mode")
    if retrieval_mode not in ("dense", "hybrid"):
        raise EvalError(
            f"配置 retrieval_mode 必须是 dense 或 hybrid，实际为 {retrieval_mode!r}：{path}"
        )

    top_k = config.get("top_k")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 50:
        raise EvalError(f"配置 top_k 必须是 1–50 的整数，实际为 {top_k!r}：{path}")

    filters = config.get("filters")
    if filters is not None and not isinstance(filters, dict):
        raise EvalError(f"配置 filters 必须是映射或 null：{path}")

    score_threshold = config.get("score_threshold")
    if score_threshold is not None and not isinstance(score_threshold, (int, float)):
        raise EvalError(f"配置 score_threshold 必须是数字或 null：{path}")

    config["name"] = name.strip()
    config.setdefault("use_cache", False)
    config.setdefault("run_chat", True)
    return config


def load_golden(path: Path, split: str) -> list[dict[str, Any]]:
    """
    读 golden JSONL 并按 split 过滤。

    每题会被补齐成统一结构：``gold_urls`` 提取自 ``evidence``，
    ``answerable`` 强制为布尔值。``evidence`` 里既接受 ``{"url": ...}``，
    也接受纯 URL 字符串（手工造题时更省事）。

    :param path: ``golden.jsonl`` 路径。
    :param split: ``dev`` / ``test`` / ``all``。
    :return: 题目列表。
    :raises EvalError: 文件缺失、JSON 非法、必填字段缺失或过滤后没有题目。
    """
    if not path.is_file():
        raise EvalError(
            f"golden set 不存在：{path}。请先由主会话造题并写入 "
            f"``evals/golden.jsonl``，或通过 --golden 指定路径。"
        )

    questions: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue

            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvalError(f"{path}:{line_number} JSON 解析失败：{exc}") from exc
            if not isinstance(row, dict):
                raise EvalError(f"{path}:{line_number} 必须是 JSON 对象")

            for field in ("id", "question"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise EvalError(f"{path}:{line_number} 缺少非空字符串字段 {field}")
            if not isinstance(row.get("answerable"), bool):
                raise EvalError(f"{path}:{line_number} answerable 必须是布尔值")

            evidence = row.get("evidence") or []
            if not isinstance(evidence, list):
                raise EvalError(f"{path}:{line_number} evidence 必须是数组")
            gold_urls: list[str] = []
            for item in evidence:
                if isinstance(item, dict):
                    url = item.get("url")
                elif isinstance(item, str):
                    url = item
                else:
                    url = None
                if isinstance(url, str) and url.strip():
                    gold_urls.append(url.strip())

            if row["answerable"] and not gold_urls:
                print(
                    f"[eval] 警告：{row['id']} 标为可回答但没有 evidence URL，"
                    "检索 / 引用指标会跳过该题",
                    file=sys.stderr,
                )

            if split != "all" and row.get("split") != split:
                continue

            questions.append(
                {
                    "id": row["id"],
                    "question": row["question"].strip(),
                    "reference_answer": row.get("reference_answer"),
                    "type": row.get("type", "unknown"),
                    "split": row.get("split"),
                    "answerable": row["answerable"],
                    "gold_urls": gold_urls,
                }
            )

    if not questions:
        raise EvalError(f"golden set 在 split={split!r} 下没有题目：{path}")
    return questions


# ---------- HTTP / SSE ----------


def _post_json(client: httpx.Client, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    POST 一个 JSON 请求并返回响应 JSON。

    :param client: 共享的 httpx 客户端。
    :param url: 完整 URL。
    :param payload: 请求体。
    :return: 解析后的 JSON。
    :raises EvalError: 网络错误或非 2xx 状态码。
    """
    try:
        response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise EvalError(f"请求 {url} 失败：{exc}") from exc
    if response.status_code >= 400:
        raise EvalError(
            f"{url} 返回 HTTP {response.status_code}：{response.text[:300]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise EvalError(f"{url} 返回的不是 JSON：{response.text[:300]}") from exc


def iter_sse_events(response: httpx.Response) -> Iterator[tuple[str, dict[str, Any]]]:
    """
    把 SSE 响应按空行切成 ``(事件名, 负载)``。

    规则与 ``app.utils.sse.format_sse`` 对应：``event:`` 给名字、``data:`` 给 JSON，
    空行是事件边界；以 ``:`` 开头的注释行忽略。

    :param response: 流式响应（调用方已用 ``client.stream`` 打开）。
    :yield: 事件名与解析后的 JSON 负载。
    :raises EvalError: 某个事件的 data 不是合法 JSON。
    """
    event_name = ""
    data_lines: list[str] = []

    def flush() -> tuple[str, dict[str, Any]] | None:
        """
        把当前累积的事件格式化成 ``(事件名, 负载)``。

        :return: 事件元组；没有累积内容时返回 ``None``。
        """
        nonlocal event_name, data_lines
        if not event_name and not data_lines:
            return None
        name = event_name or "message"
        raw = "\n".join(data_lines)
        event_name, data_lines = "", []
        if not raw:
            return name, {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EvalError(f"SSE 事件 {name!r} 的 data 不是合法 JSON：{raw[:300]}") from exc
        return name, parsed if isinstance(parsed, dict) else {"data": parsed}

    for line in response.iter_lines():
        if line == "":
            event = flush()
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    event = flush()
    if event is not None:
        yield event


def run_search(
    client: httpx.Client,
    base_url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    调一次 ``/search``，返回结果列表与耗时等元数据。

    :param client: 共享的 httpx 客户端。
    :param base_url: rag-api 基址。
    :param payload: 检索请求体。
    :return: ``{"results": [...], "latency_ms": ..., "embedding_model": ..., ...}``。
    """
    data = _post_json(client, base_url.rstrip("/") + SEARCH_PATH, payload)
    results = data.get("results")
    if not isinstance(results, list):
        raise EvalError(f"/search 响应缺少 results 数组：{str(data)[:300]}")
    return {
        "results": results,
        "latency_ms": float(data.get("latency_ms") or 0),
        "embedding_model": data.get("embedding_model"),
        "retrieval_mode": data.get("retrieval_mode"),
        "score_type": data.get("score_type"),
        "best_dense_score": data.get("best_dense_score"),
        "keyword_hits": data.get("keyword_hits"),
    }


def stream_chat(
    client: httpx.Client,
    base_url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """
    调一次 ``/chat/stream`` 并解析 SSE。

    收集 ``citations`` 里的 URL、``token`` 拼成答案、``done`` 的统计与标记。
    首 token / 总延迟优先取 ``done.latency_ms``（服务端口径），缺失时回退到客户端计时。

    :param client: 共享的 httpx 客户端。
    :param base_url: rag-api 基址。
    :param payload: 问答请求体。
    :return: 单次问答的原始记录。
    :raises EvalError: 网络错误、非 2xx、或流里既没有 done 也没有 error。
    """
    started = time.perf_counter()
    client_first_token_ms: float | None = None
    citations: list[dict[str, Any]] = []
    tokens: list[str] = []
    done: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    try:
        with client.stream(
            "POST", base_url.rstrip("/") + CHAT_PATH, json=payload
        ) as response:
            if response.status_code >= 400:
                body = response.read().decode("utf-8", "replace")
                raise EvalError(
                    f"{CHAT_PATH} 返回 HTTP {response.status_code}：{body[:300]}"
                )
            for event_name, data in iter_sse_events(response):
                if event_name == "citations":
                    raw = data.get("citations")
                    if isinstance(raw, list):
                        citations = raw
                elif event_name == "token":
                    text = data.get("text")
                    if not isinstance(text, str):
                        continue
                    if client_first_token_ms is None:
                        client_first_token_ms = (time.perf_counter() - started) * 1000
                    tokens.append(text)
                elif event_name == "done":
                    done = data
                elif event_name == "error":
                    error = data
    except httpx.HTTPError as exc:
        raise EvalError(f"请求 {CHAT_PATH} 失败：{exc}") from exc

    client_total_ms = (time.perf_counter() - started) * 1000
    if done is None and error is None:
        raise EvalError(f"{CHAT_PATH} 的 SSE 流没有 done / error 事件（连接提前关闭？）")

    done = done or {}
    latency = done.get("latency_ms") or {}
    server_first_token = float(latency.get("first_token") or 0)
    server_total = float(latency.get("total") or 0)

    return {
        "answer": "".join(tokens),
        "cited_urls": [
            item["url"]
            for item in citations
            if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"]
        ],
        "first_token_ms": server_first_token or (client_first_token_ms or 0.0),
        "total_ms": server_total or client_total_ms,
        "cache_hit": bool(done.get("cache_hit")),
        "degraded": bool(done.get("degraded")),
        "grounded": done.get("grounded"),
        "model": done.get("model"),
        "reason": done.get("reason"),
        "invalid_citations": done.get("invalid_citations"),
        "usage": done.get("usage"),
        "retrieval_mode": done.get("retrieval_mode"),
        "score_type": done.get("score_type"),
        "best_dense_score": done.get("best_dense_score"),
        "keyword_hits": done.get("keyword_hits"),
        "stream_error": error,
    }


# ---------- 单题评测 ----------


def build_payloads(
    config: dict[str, Any],
    question: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    按配置拼出检索与问答请求体（两者共用同一组覆盖参数）。

    :param config: 评测配置。
    :param question: 题目文本。
    :return: ``(search_payload, chat_payload)``。
    """
    common = {
        "top_k": config["top_k"],
        "filters": config.get("filters"),
        "retrieval_mode": config["retrieval_mode"],
        "use_cache": bool(config.get("use_cache", False)),
        "score_threshold": config.get("score_threshold"),
    }
    search_payload = {"query": question, **common}
    chat_payload = {"question": question, **common}
    return search_payload, chat_payload


def detect_refusal(answer: str, cited_urls: list[str], reason: str | None) -> bool:
    """
    判定一次回答是否属于拒答。

    命中任一条件即算拒答：

    - 答案包含固定拒答话术「根据现有资料无法确定」；
    - ``done.reason`` 为 ``no_context`` / ``low_score`` 且 citations 为空。

    :param answer: 完整答案文本。
    :param cited_urls: 回答实际引用的 URL。
    :param reason: ``done.reason``（可能为空）。
    :return: 是否拒答。
    """
    if any(marker in answer for marker in REFUSAL_MARKERS):
        return True
    return reason in REFUSAL_REASONS and not cited_urls


def evaluate_question(
    client: httpx.Client,
    base_url: str,
    config: dict[str, Any],
    item: dict[str, Any],
    *,
    run_chat: bool,
    runs: int,
) -> dict[str, Any]:
    """
    对单题做 warm-up + ``runs`` 次正式运行，返回明细。

    :param client: 共享的 httpx 客户端。
    :param base_url: rag-api 基址。
    :param config: 评测配置。
    :param item: golden 题。
    :param run_chat: 是否跑问答（``--retrieval-only`` 时为 ``False``）。
    :param runs: 正式运行次数（不含 warm-up）。
    :return: 单题明细（含检索结果、指标与问答记录）。
    """
    search_payload, chat_payload = build_payloads(config, item["question"])
    top_k = config["top_k"]

    # warm-up：只把连接 / 模型 / 缓存路径跑热，不计入任何指标
    run_search(client, base_url, search_payload)
    if run_chat:
        stream_chat(client, base_url, chat_payload)

    search_runs = [run_search(client, base_url, search_payload) for _ in range(runs)]
    chat_runs = (
        [stream_chat(client, base_url, chat_payload) for _ in range(runs)]
        if run_chat
        else []
    )

    last_search = search_runs[-1]
    # 检索 URL：按结果顺序取 URL 非空的前 top_k 条
    retrieved_urls = [
        result["url"]
        for result in last_search["results"]
        if isinstance(result.get("url"), str) and result["url"]
    ][:top_k]

    gold_urls = item["gold_urls"]
    has_gold = bool(gold_urls)
    metrics: dict[str, float | None] = {
        "recall_at_5": recall_at_k([retrieved_urls], [gold_urls], RECALL_K)
        if has_gold
        else None,
        "evidence_recall_at_5": evidence_recall_at_k(
            [retrieved_urls], [gold_urls], RECALL_K
        )
        if has_gold
        else None,
        "mrr": mrr([retrieved_urls], [gold_urls], top_k) if has_gold else None,
        "ndcg_at_10": ndcg_at_k([retrieved_urls], [gold_urls], NDCG_K)
        if has_gold
        else None,
    }

    detail: dict[str, Any] = {
        "id": item["id"],
        "question": item["question"],
        "type": item["type"],
        "split": item["split"],
        "answerable": item["answerable"],
        "gold_urls": gold_urls,
        "reference_answer": item["reference_answer"],
        "retrieved_urls": retrieved_urls,
        "retrieval": {
            "latency_ms_p50": float(statistics.median([r["latency_ms"] for r in search_runs])),
            "embedding_model": last_search["embedding_model"],
            "retrieval_mode": last_search["retrieval_mode"],
            "score_type": last_search["score_type"],
            "best_dense_score": last_search["best_dense_score"],
            "keyword_hits": last_search["keyword_hits"],
        },
        "metrics": metrics,
        "chat": None,
    }

    if chat_runs:
        last_chat = chat_runs[-1]
        precision, recall = citation_precision_recall(
            last_chat["cited_urls"], gold_urls
        )
        refused = detect_refusal(
            last_chat["answer"], last_chat["cited_urls"], last_chat["reason"]
        )
        detail["chat"] = {
            "answer": last_chat["answer"],
            "cited_urls": last_chat["cited_urls"],
            "refused": refused,
            "citation_precision": precision if last_chat["cited_urls"] else None,
            "citation_recall": recall if last_chat["cited_urls"] else None,
            "first_token_ms_p50": float(
                statistics.median([r["first_token_ms"] for r in chat_runs])
            ),
            "total_ms_p50": float(statistics.median([r["total_ms"] for r in chat_runs])),
            "cache_hit_runs": sum(1 for r in chat_runs if r["cache_hit"]),
            "degraded_runs": sum(1 for r in chat_runs if r["degraded"]),
            "grounded": last_chat["grounded"],
            "model": last_chat["model"],
            "reason": last_chat["reason"],
            "invalid_citations": last_chat["invalid_citations"],
            "usage": last_chat["usage"],
            "stream_error": last_chat["stream_error"],
        }

    return detail


# ---------- 汇总 ----------


def summarize(
    details: list[dict[str, Any]],
    *,
    top_k: int,
    run_chat: bool,
) -> dict[str, Any]:
    """
    汇总全部题目的指标。

    检索指标只在「可回答且标了 gold URL」的题上计算；引用指标在「可回答 + 有引用」的
    题上取平均，并单列引用覆盖率，避免用跳过空引用来美化准确率。

    :param details: 单题明细列表。
    :param top_k: 本次配置的检索深度（MRR 用）。
    :param run_chat: 是否跑了问答。
    :return: 分组的汇总指标。
    """
    retrieval_items = [
        detail
        for detail in details
        if detail["answerable"] and detail["gold_urls"]
    ]
    retrieved_matrix = [detail["retrieved_urls"] for detail in retrieval_items]
    gold_matrix = [detail["gold_urls"] for detail in retrieval_items]

    retrieval_summary = {
        "recall_at_5": recall_at_k(retrieved_matrix, gold_matrix, RECALL_K),
        "evidence_recall_at_5": evidence_recall_at_k(
            retrieved_matrix, gold_matrix, RECALL_K
        ),
        "mrr": mrr(retrieved_matrix, gold_matrix, top_k),
        "ndcg_at_10": ndcg_at_k(retrieved_matrix, gold_matrix, NDCG_K),
        "n_questions": len(retrieval_items),
    }

    generation_summary: dict[str, Any] | None = None
    performance: dict[str, float | None] = {
        "retrieval_p50": percentile(
            [detail["retrieval"]["latency_ms_p50"] for detail in details], 50
        ),
        "retrieval_p95": percentile(
            [detail["retrieval"]["latency_ms_p50"] for detail in details], 95
        ),
        "first_token_p50": None,
        "first_token_p95": None,
        "chat_total_p50": None,
        "chat_total_p95": None,
    }

    if run_chat:
        chat_items = [detail for detail in details if detail["chat"] is not None]
        answerable_items = [
            detail
            for detail in details
            if detail["answerable"] and detail["gold_urls"]
        ]
        cited_items = [
            detail
            for detail in answerable_items
            if detail["chat"] and detail["chat"]["cited_urls"]
        ]
        citation_precisions = [
            detail["chat"]["citation_precision"]
            for detail in cited_items
            if detail["chat"]["citation_precision"] is not None
        ]
        citation_recalls = [
            detail["chat"]["citation_recall"]
            for detail in cited_items
            if detail["chat"]["citation_recall"] is not None
        ]
        refusals = refusal_metrics(
            [detail["answerable"] for detail in details],
            [bool(detail["chat"]["refused"]) for detail in details],
        )

        generation_summary = {
            "citation_precision": (
                sum(citation_precisions) / len(citation_precisions)
                if citation_precisions
                else None
            ),
            "citation_recall": (
                sum(citation_recalls) / len(citation_recalls)
                if citation_recalls
                else None
            ),
            "citation_coverage": (
                len(cited_items) / len(answerable_items) if answerable_items else 0.0
            ),
            "refusal_accuracy": refusals["refusal_accuracy"],
            "false_refusal_rate": refusals["false_refusal_rate"],
            "n_negative": sum(1 for detail in details if not detail["answerable"]),
            "n_positive": sum(1 for detail in details if detail["answerable"]),
            "n_answers_with_citations": len(cited_items),
            "degraded_rate": (
                sum(
                    1
                    for detail in chat_items
                    if detail["chat"]["degraded_runs"] > 0
                )
                / len(chat_items)
                if chat_items
                else 0.0
            ),
            "cache_hit_runs": sum(
                detail["chat"]["cache_hit_runs"] for detail in chat_items
            ),
        }
        first_tokens = [detail["chat"]["first_token_ms_p50"] for detail in chat_items]
        totals = [detail["chat"]["total_ms_p50"] for detail in chat_items]
        performance["first_token_p50"] = percentile(first_tokens, 50)
        performance["first_token_p95"] = percentile(first_tokens, 95)
        performance["chat_total_p50"] = percentile(totals, 50)
        performance["chat_total_p95"] = percentile(totals, 95)

    return {
        "retrieval": retrieval_summary,
        "generation": generation_summary,
        "performance_ms": performance,
    }


def build_environment(
    args: argparse.Namespace,
    config: dict[str, Any],
    details: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    收集可复现所需的环境信息。

    ``embedding_model`` 取自 ``SearchResponse``；``llm_model`` 取自 ``done.model``
    （extractive 兜底时会如实是 ``extractive``）。``prompt_version`` 读环境变量
    ``PROMPT_VERSION``，没有就写 ``unknown``。

    :param args: 命令行参数。
    :param config: 评测配置。
    :param details: 单题明细（用于抽取实际模型名）。
    :return: 环境字典。
    """
    embedding_models = {
        detail["retrieval"]["embedding_model"]
        for detail in details
        if detail["retrieval"]["embedding_model"]
    }
    llm_models = {
        detail["chat"]["model"]
        for detail in details
        if detail["chat"] and detail["chat"]["model"]
    }

    return {
        "date": date.today().isoformat(),
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "base_url": args.base_url,
        "config": config["name"],
        "retrieval_mode": config["retrieval_mode"],
        "split": args.split,
        "question_count": len(details),
        "runs": args.runs,
        # 评测按题串行执行：这台机器上并发会让延迟口径失真，先固定为 1
        "concurrency": 1,
        "top_k": config["top_k"],
        "candidate_k": config.get("candidate_k"),
        "use_cache": bool(config.get("use_cache", False)),
        "score_threshold": config.get("score_threshold"),
        "filters": config.get("filters"),
        "retrieval_only": args.retrieval_only,
        "embedding_model": sorted(embedding_models)[0] if embedding_models else "unknown",
        "embedding_models_seen": sorted(embedding_models),
        "llm_model": sorted(llm_models)[0] if llm_models else "unknown",
        "llm_provider": "extractive" if llm_models == {"extractive"} else "unknown",
        "prompt_version": os.environ.get("PROMPT_VERSION", "unknown"),
        "python": platform.python_version(),
    }


def _git_commit() -> str:
    """
    取当前仓库短 commit；不在 git 仓库（例如容器镜像内）时如实返回 ``unknown``。

    :return: 短 commit hash 或 ``unknown``。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else "unknown"


# ---------- 报告 ----------


def _round(value: float | None, digits: int = 6) -> float | None:
    """
    统一 JSON 里浮点数的精度，避免一长串二进制尾数。

    :param value: 原始值（可为 ``None``）。
    :param digits: 保留小数位。
    :return: 四舍五入后的值。
    """
    return round(value, digits) if isinstance(value, (int, float)) else value


def pick_failures(details: list[dict[str, Any]], limit: int = 5) -> list[tuple[str, dict]]:
    """
    自动摘取值得人工看的失败样例。

    优先级：负样本未拒答 → 正样本误拒 → 检索未命中。最多返回 ``limit`` 条。

    :param details: 单题明细。
    :param limit: 最多返回几条。
    :return: ``(失败类型, 明细)`` 列表。
    """
    buckets: dict[str, list[dict[str, Any]]] = {
        "负样本未拒答": [],
        "正样本误拒": [],
        "检索未命中": [],
    }
    for detail in details:
        chat = detail["chat"]
        if not detail["answerable"]:
            if chat is not None and not chat["refused"]:
                buckets["负样本未拒答"].append(detail)
            continue
        if chat is not None and chat["refused"]:
            buckets["正样本误拒"].append(detail)
        elif detail["metrics"]["recall_at_5"] == 0:
            buckets["检索未命中"].append(detail)

    failures = [
        (kind, detail)
        for kind in ("负样本未拒答", "正样本误拒", "检索未命中")
        for detail in buckets[kind]
    ]
    return failures[:limit]


def _fmt_rate(value: float | None) -> str:
    """
    百分比格式化；未测的指标写「未测」，绝不填空数字。

    :param value: 0–1 的比例。
    :return: Markdown 单元格文本。
    """
    return "未测" if value is None else f"{value * 100:.1f}%"


def _fmt_ms(value: float | None) -> str:
    """
    延迟格式化（毫秒，取整）。

    :param value: 毫秒值。
    :return: Markdown 单元格文本。
    """
    return "未测" if value is None else f"{value:.0f}"


def _clip(text: Any, limit: int = 160) -> str:
    """
    截断长文本，避免报告被整段回答撑爆。

    :param text: 任意文本。
    :param limit: 最大字符数。
    :return: 截断后的单行文本。
    """
    value = "" if text is None else str(text)
    value = value.replace("\n", " ").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def render_markdown(result: dict[str, Any]) -> str:
    """
    渲染可贴进报告的 Markdown。

    :param result: runner 的完整结果字典。
    :return: Markdown 文本。
    """
    environment = result["environment"]
    summary = result["summary"]
    retrieval = summary["retrieval"]
    generation = summary["generation"]
    performance = summary["performance_ms"]

    lines: list[str] = [
        f"# RAG 评测报告（{environment['date']}） - {environment['config']}",
        "",
        "## 环境",
        "",
        f"- 语料：<待填>（active chunk 数 / 文档数由主会话补充，JSON 的 ``corpus`` 字段同）",
        f"- embedding：{environment['embedding_model']}",
        f"- LLM：{environment['llm_model']}（provider：{environment['llm_provider']}）",
        f"- prompt 版本：{environment['prompt_version']}",
        f"- git commit：{environment['git_commit']}",
        f"- 服务地址：{environment['base_url']}",
        f"- 配置：{environment['config']}（retrieval_mode={environment['retrieval_mode']}，"
        f"top_k={environment['top_k']}，candidate_k={environment['candidate_k']}，"
        f"use_cache={environment['use_cache']}，score_threshold={environment['score_threshold']}）",
        f"- 题集：split={environment['split']}，n={environment['question_count']}，"
        f"每题 warm-up 1 次 + 正式 {environment['runs']} 次取中位数，"
        f"并发 {environment['concurrency']}",
        f"- 时间：{environment['timestamp']}",
        "",
        f"## 结果（{environment['split']} 集，n={environment['question_count']}）",
        "",
        "| 指标 | 值 |",
        "|---|---:|",
        f"| Recall@5 | {_fmt_rate(retrieval['recall_at_5'])} |",
        f"| Evidence Recall@5 | {_fmt_rate(retrieval['evidence_recall_at_5'])} |",
        f"| MRR（top-{environment['top_k']}） | {_fmt_rate(retrieval['mrr'])} |",
        f"| nDCG@10 | {_fmt_rate(retrieval['ndcg_at_10'])} |",
    ]

    if generation is None:
        lines.append("| 引用准确率 / 召回率 | 未测（--retrieval-only） |")
        lines.append("| 拒答准确率 / 误拒率 | 未测（--retrieval-only） |")
    else:
        lines.append(
            f"| 引用准确率 | {_fmt_rate(generation['citation_precision'])}"
            f"（有引用的 {generation['n_answers_with_citations']} 题，"
            f"覆盖率 {_fmt_rate(generation['citation_coverage'])}） |"
        )
        lines.append(f"| 引用召回率 | {_fmt_rate(generation['citation_recall'])} |")
        lines.append(
            f"| 拒答准确率 | {_fmt_rate(generation['refusal_accuracy'])}"
            f"（负样本 {generation['n_negative']} 题） |"
        )
        lines.append(
            f"| 误拒率 | {_fmt_rate(generation['false_refusal_rate'])}"
            f"（正样本 {generation['n_positive']} 题） |"
        )
        lines.append(f"| 降级率 | {_fmt_rate(generation['degraded_rate'])} |")

    lines += [
        "",
        "### 延迟（毫秒）",
        "",
        "| 指标 | p50 | p95 |",
        "|---|---:|---:|",
        f"| 检索 | {_fmt_ms(performance['retrieval_p50'])} | "
        f"{_fmt_ms(performance['retrieval_p95'])} |",
        f"| 首 token | {_fmt_ms(performance['first_token_p50'])} | "
        f"{_fmt_ms(performance['first_token_p95'])} |",
        f"| 总延迟 | {_fmt_ms(performance['chat_total_p50'])} | "
        f"{_fmt_ms(performance['chat_total_p95'])} |",
        "",
        "## 失败样例（自动摘取）",
        "",
    ]

    failures = pick_failures(result["questions"])
    if not failures:
        lines.append("本轮没有自动摘取到失败样例（仍建议人工抽检）。")
    for reason, detail in failures:
        lines.append(f"- **{detail['id']}**（{reason}，{detail['type']}）：{detail['question']}")
        lines.append(f"  - gold：{', '.join(detail['gold_urls']) or '（无）'}")
        lines.append(f"  - 检索：{', '.join(detail['retrieved_urls']) or '（无）'}")
        if detail["chat"] is not None:
            lines.append(
                f"  - 回答：{_clip(detail['chat']['answer']) or '（空）'}"
            )
            if detail["chat"]["stream_error"]:
                lines.append(f"  - SSE error：{_clip(detail['chat']['stream_error'])}")

    lines += [
        "",
        "## 口径说明",
        "",
        "- 指标按 evidence URL 匹配；URL 去 fragment、去尾斜杠、host 小写后比较。",
        f"- 检索指标只在可回答且带 evidence 的题上统计；MRR 在配置的 top_k="
        f"{environment['top_k']} 内计算。",
        "- 每题先 warm-up 1 次（不计分），再正式跑 runs 次：延迟取每题的中位数，"
        "再对题求 p50 / p95；指标内容取最后一次运行（缓存关闭，结果稳定）。",
        "- nDCG@10 的二值相关性按 URL 去重；若 top_k < 10，实际只有 top_k 条参与。",
        "- 引用准确率 / 召回率在「可回答且确实给出引用」的题上取平均；空引用题不计入"
        "平均，但计入引用覆盖率。",
        "- 拒答判定：答案含「根据现有资料无法确定」，或 done.reason ∈ "
        "{no_context, low_score} 且 citations 为空。",
        "- 不统计答案正确率：本地小模型 / extractive 兜底不足以自动判分。",
        "- 缓存已关闭（use_cache=false）；若 JSON 里 cache_hit_runs > 0，说明配置没生效。",
        "",
        "## 结论",
        "",
        "- <待主会话对照 dense / hybrid 后填写>",
        "",
    ]
    return "\n".join(lines)


def write_outputs(result: dict[str, Any], out_base: Path) -> tuple[Path, Path]:
    """
    写 JSON 与 Markdown 两个文件（同名、不同后缀）。

    :param result: 完整结果字典。
    :param out_base: 输出基名（不带后缀）。
    :return: ``(json_path, markdown_path)``。
    """
    if out_base.suffix in (".json", ".md"):
        out_base = out_base.with_suffix("")
    out_base.parent.mkdir(parents=True, exist_ok=True)

    json_path = out_base.with_suffix(".json")
    markdown_path = out_base.with_suffix(".md")
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    markdown_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, markdown_path


# ---------- 入口 ----------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    解析命令行参数。

    :param argv: 参数列表；``None`` 表示取 ``sys.argv``。
    :return: 解析结果。
    """
    parser = argparse.ArgumentParser(
        description="对 rag-api 跑 RAG 评测（检索 + 流式问答），输出 JSON 与 Markdown"
    )
    parser.add_argument(
        "--config",
        required=True,
        help="配置短名（dense / hybrid）或 YAML 路径",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"rag-api 基址（默认 {DEFAULT_BASE_URL}，容器内用服务名；宿主机用 8001）",
    )
    parser.add_argument(
        "--golden",
        default=str(DEFAULT_GOLDEN),
        help=f"golden JSONL 路径（默认 {DEFAULT_GOLDEN}）",
    )
    parser.add_argument(
        "--split",
        choices=("dev", "test", "all"),
        default="test",
        help="按题目的 split 字段过滤（默认 test）",
    )
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="只跑 /search，不调问答（更快，先看召回）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出基名（默认 evals/results/YYYY-MM-DD-<config>，会生成 .json / .md）",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=2,
        help="每题正式运行次数（不含 warm-up，默认 2）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="单请求超时秒数（默认 30）",
    )
    return parser.parse_args(argv)


def evaluate(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    """
    跑完整评测并返回结果与输出基名。

    :param args: 命令行参数。
    :return: ``(result, out_base)``。
    :raises EvalError: 配置 / 题集 / HTTP 可预期失败。
    """
    if args.runs < 1:
        raise EvalError(f"--runs 必须 ≥ 1，实际为 {args.runs}")

    config = load_config(args.config)
    questions = load_golden(Path(args.golden), args.split)
    run_chat = bool(config.get("run_chat", True)) and not args.retrieval_only

    details: list[dict[str, Any]] = []
    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        for index, item in enumerate(questions, start=1):
            print(
                f"[eval] ({index}/{len(questions)}) {item['id']} {item['question'][:40]}",
                file=sys.stderr,
            )
            detail = evaluate_question(
                client,
                args.base_url,
                config,
                item,
                run_chat=run_chat,
                runs=args.runs,
            )
            if detail["chat"] and detail["chat"]["stream_error"]:
                print(
                    f"[eval] 警告：{item['id']} SSE 返回 error 事件："
                    f"{detail['chat']['stream_error']}",
                    file=sys.stderr,
                )
            details.append(detail)

    summary = summarize(details, top_k=config["top_k"], run_chat=run_chat)
    environment = build_environment(args, config, details)

    # 浮点数统一收一下精度，报告与 JSON 都不出现二进制尾数
    for key, value in summary["retrieval"].items():
        summary["retrieval"][key] = _round(value) if isinstance(value, float) else value
    if summary["generation"] is not None:
        for key, value in summary["generation"].items():
            if isinstance(value, float):
                summary["generation"][key] = _round(value)
    for key, value in summary["performance_ms"].items():
        summary["performance_ms"][key] = _round(value)

    result = {
        "environment": environment,
        "config": config,
        # 语料计数不直连 MySQL（rag 角色没有 stats 接口）；由主会话在报告里补
        "corpus": {},
        "summary": summary,
        "questions": details,
    }

    if args.out:
        out_base = Path(args.out)
    else:
        out_base = RESULTS_DIR / f"{date.today().isoformat()}-{config['name']}"

    return result, out_base


def main(argv: list[str] | None = None) -> None:
    """
    命令行入口：解析参数 → 评测 → 落盘，失败统一以退出码 2 结束。

    :param argv: 参数列表；``None`` 表示取 ``sys.argv``。
    """
    args = parse_args(argv)
    try:
        result, out_base = evaluate(args)
        json_path, markdown_path = write_outputs(result, out_base)
    except EvalError as exc:
        print(f"[eval] 失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except Exception as exc:  # noqa: BLE001 - 脚本入口：打印清楚再以非零码退出
        print(f"[eval] 未预期错误：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    print(f"[eval] JSON：{json_path}", file=sys.stderr)
    print(f"[eval] Markdown：{markdown_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
