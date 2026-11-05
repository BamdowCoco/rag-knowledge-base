"""
演示链路冒烟：健康检查 → 入库 → 等向量化 → 检索 → 流式问答。

【它验什么】把 demo 的主链路串起来跑一遍，哪一段断了就停在那一行并给出原因：

- ``GET /health``：摄入进程与问答进程都活着；
- ``POST /api/v1/documents``：标准化、切片、落库通；
- 轮询 ``/documents/{id}/chunks``：embedder 认领、写 Qdrant、回报的状态流转通；
- ``POST /api/v1/search``：Qdrant 能召回；
- ``POST /api/v1/chat/stream``：SSE 的 ``citations`` / ``token`` / ``done`` 都到齐。

【怎么跑】默认在宿主机跑（两个端口都由 compose 映射到本机）：

    python scripts/smoke.py
    python scripts/smoke.py --skip-ingest      # 样例文档已在，只验检索与问答
    python scripts/smoke.py --timeout 180      # 首次跑 embedding 慢时放宽等待

失败时退出码为 1，方便接进脚本或 CI。

【已知副作用】不加 ``--skip-ingest`` 时每次都会 POST 一次 ``scripts/example-doc.json``。
摄入接口对「同一 ``source`` + ``external_id`` 且内容未变」返回 200 并回已有版本，不算错误，
所以重复跑是安全的；想换一篇就改那个 JSON 的 ``external_id``。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

# 管道 / 重定向时 Windows 默认用 GBK 写 stdout，和 UTF-8 的终端 / 日志对不上；统一成 UTF-8。
# 真实控制台下 Python 本来就是 UTF-8，这里等于不做事。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

try:
    import httpx
except ImportError:  # pragma: no cover - 只影响本地直接运行
    sys.exit("缺少 httpx：先 pip install -r requirements-dev.txt，或确认已进虚拟环境。")

EXAMPLE_DOC_PATH = Path(__file__).resolve().parent / "example-doc.json"
POLL_INTERVAL_SECONDS = 2.0
CHUNK_PAGE_LIMIT = 100


class SmokeError(RuntimeError):
    """单个步骤失败；由 ``attempt`` 统一打印成 FAIL 行。"""


class SmokeAbort(Exception):
    """某步失败后终止后续步骤（原因已经打印过）。"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """
    解析命令行参数。

    :param argv: 参数列表；``None`` 时读 ``sys.argv``。
    :return: 解析结果。
    """
    parser = argparse.ArgumentParser(
        description="RAG demo smoke test: health, ingest, embed, search, chat SSE."
    )
    parser.add_argument(
        "--ingest-base",
        default="http://localhost:8000",
        help="摄入 API 基址（默认 http://localhost:8000）",
    )
    parser.add_argument(
        "--chat-base",
        default="http://localhost:8001",
        help="检索 / 问答 API 基址（默认 http://localhost:8001）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="单次请求与等待向量化的超时秒数（默认 90）",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="跳过 POST 入库，改为查找已有的样例文档",
    )
    return parser.parse_args(argv)


def load_example_doc() -> dict[str, Any]:
    """
    读取样例文档。

    用 ``__file__`` 定位，不依赖当前工作目录 —— 从仓库根或 scripts 目录里跑都一样。

    :return: 样例 JSON（直接作为 ``POST /api/v1/documents`` 的请求体）。
    :raises SmokeError: 文件缺失、读不了或不是合法 JSON。
    """
    try:
        return json.loads(EXAMPLE_DOC_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SmokeError(f"读取样例失败 {EXAMPLE_DOC_PATH}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise SmokeError(f"样例不是合法 JSON：{exc}") from exc


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    expected: tuple[int, ...] = (200,),
    **kwargs: Any,
) -> Any:
    """
    发一个请求并返回解析后的 JSON。

    :param client: 复用的 httpx 客户端。
    :param method: HTTP 方法。
    :param url: 完整 URL。
    :param expected: 可接受的状态码。
    :param kwargs: 透传给 ``client.request``（``json`` / ``params`` 等）。
    :return: 响应体（已 JSON 解析）。
    :raises SmokeError: 网络错误、状态码不在 ``expected`` 或响应不是 JSON。
    """
    try:
        response = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise SmokeError(f"请求 {url} 失败：{exc}") from exc
    if response.status_code not in expected:
        body = response.text[:300].replace("\n", " ")
        raise SmokeError(f"{method} {url} 返回 HTTP {response.status_code}：{body}")
    try:
        return response.json()
    except ValueError as exc:
        raise SmokeError(f"{method} {url} 返回的不是 JSON：{response.text[:200]}") from exc


def check_health(client: httpx.Client, base: str) -> None:
    """
    检查 ``/health``。

    :param client: httpx 客户端。
    :param base: API 基址。
    :raises SmokeError: 状态码不是 200 或响应体不是 ``{"status": "ok"}``。
    """
    payload = request_json(client, "GET", f"{base}/health")
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise SmokeError(f"{base}/health 返回异常：{payload!r}")


def ingest_document(
    client: httpx.Client, base: str, doc: dict[str, Any]
) -> dict[str, Any]:
    """
    提交样例文档。

    新建返回 201，内容未变的重复提交返回 200（同 ``external_id``），两种都算成功。

    :param client: httpx 客户端。
    :param base: 摄入 API 基址。
    :param doc: 文档请求体。
    :return: 文档出参（至少含 ``id``）。
    :raises SmokeError: 接口失败或响应缺少 ``id``。
    """
    payload = request_json(
        client, "POST", f"{base}/api/v1/documents", json=doc, expected=(200, 201)
    )
    if not isinstance(payload, dict) or "id" not in payload:
        raise SmokeError(f"入库响应缺少 id：{payload!r}")
    return payload


def find_document_id(
    client: httpx.Client, base: str, doc: dict[str, Any]
) -> int | None:
    """
    在文档列表里按 ``source`` + ``external_id`` 找已有样例。

    标题关键词也带上，避免 syncer 持续写入后样例被挤出第一页。

    :param client: httpx 客户端。
    :param base: 摄入 API 基址。
    :param doc: 样例文档请求体。
    :return: 文档 id；找不到则为 ``None``。
    """
    params: dict[str, Any] = {"limit": CHUNK_PAGE_LIMIT}
    if doc.get("source"):
        params["source"] = doc["source"]
    title = str(doc.get("title") or "").strip()
    if title:
        # 列表接口的 q 最长 50 字符
        params["q"] = title[:50]

    page = request_json(client, "GET", f"{base}/api/v1/documents", params=params)
    for item in page.get("items") or []:
        if item.get("external_id") == doc.get("external_id"):
            return int(item["id"])
    return None


def wait_for_chunks(
    client: httpx.Client, base: str, document_id: int, timeout: float
) -> list[dict[str, Any]]:
    """
    轮询分块，直到全部 ``embedding_status == "done"``。

    :param client: httpx 客户端。
    :param base: 摄入 API 基址。
    :param document_id: 文档 id。
    :param timeout: 最长等待秒数。
    :return: 全部分块。
    :raises SmokeError: 没有分块、出现 ``failed`` 终态或超时。
    """
    url = f"{base}/api/v1/documents/{document_id}/chunks"
    deadline = time.monotonic() + timeout
    last_summary: dict[str, int] | None = None
    warned_partial = False

    while True:
        page = request_json(client, "GET", url, params={"limit": CHUNK_PAGE_LIMIT})
        items = page.get("items") or []
        total = int(page.get("total") or 0)
        if total == 0 or not items:
            raise SmokeError("文档没有分块：检查标准化 / 切片日志")

        if total > len(items) and not warned_partial:
            print(f"  [WARN] 分块共 {total} 条，这里只检查第一页 {len(items)} 条")
            warned_partial = True

        statuses = [str(item.get("embedding_status")) for item in items]
        summary = {status: statuses.count(status) for status in sorted(set(statuses))}
        if summary != last_summary:
            print(f"  分块状态：{summary}")
            last_summary = summary

        failed = [item["id"] for item in items if item.get("embedding_status") == "failed"]
        if failed:
            raise SmokeError(f"分块进入 failed 终态：{failed[:10]}（看 embedder 日志）")

        if all(status == "done" for status in statuses):
            return items

        if time.monotonic() >= deadline:
            raise SmokeError(
                f"等待 {timeout:.0f}s 后仍有分块未 done：{summary}（看 embedder 日志）"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def search_chunks(
    client: httpx.Client, base: str, question: str
) -> list[dict[str, Any]]:
    """
    调 ``POST /api/v1/search`` 并断言有召回。

    :param client: httpx 客户端。
    :param base: 检索 / 问答 API 基址。
    :param question: 查询文本。
    :return: 命中列表（非空）。
    :raises SmokeError: 结果为空。
    """
    payload = request_json(
        client,
        "POST",
        f"{base}/api/v1/search",
        json={"query": question, "top_k": 5},
    )
    results = payload.get("results") or []
    if not results:
        raise SmokeError("检索结果为空：Qdrant 可能还没有这篇文档的向量")
    return results


def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """
    把 SSE 行流切成 ``(event, data)``。

    事件以空行分隔；兼容 ``event:`` / ``data:`` 后带一个空格，也忽略 ``:`` 注释行。

    :param lines: 逐行文本（``httpx.Response.iter_lines()``）。
    :yield: 事件名与 ``data`` 拼接后的负载。
    """
    event = "message"
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            if data_lines:
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if data_lines:
        yield event, "\n".join(data_lines)


def parse_sse_data(data: str, event: str) -> dict[str, Any]:
    """
    解析单个 SSE 事件的 JSON 负载。

    :param data: ``data:`` 行拼接后的文本。
    :param event: 事件名（只用于报错）。
    :return: JSON 对象。
    :raises SmokeError: 不是 JSON 对象。
    """
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise SmokeError(f"SSE 事件 {event} 的 data 不是 JSON：{data[:200]}") from exc
    if not isinstance(payload, dict):
        raise SmokeError(f"SSE 事件 {event} 的负载不是对象：{payload!r}")
    return payload


def stream_chat(
    client: httpx.Client, base: str, question: str
) -> dict[str, Any]:
    """
    调流式问答并收集事件。

    :param client: httpx 客户端。
    :param base: 检索 / 问答 API 基址。
    :param question: 问题。
    :return: ``citations`` / ``token_count`` / ``token_chars`` / ``done`` 的汇总。
    :raises SmokeError: HTTP 失败、收到 ``error`` 事件或缺少三类事件。
    """
    seen: set[str] = set()
    citations: list[dict[str, Any]] = []
    token_count = 0
    token_chars = 0
    done: dict[str, Any] | None = None
    errors: list[dict[str, Any]] = []

    try:
        with client.stream(
            "POST",
            f"{base}/api/v1/chat/stream",
            json={"question": question, "top_k": 5},
        ) as response:
            if response.status_code != 200:
                body = response.read().decode("utf-8", "replace")[:300]
                raise SmokeError(
                    f"POST {base}/api/v1/chat/stream 返回 HTTP {response.status_code}：{body}"
                )
            for event, data in iter_sse(response.iter_lines()):
                payload = parse_sse_data(data, event)
                seen.add(event)
                if event == "citations":
                    citations = list(payload.get("citations") or [])
                elif event == "token":
                    token_count += 1
                    token_chars += len(str(payload.get("text", "")))
                elif event == "done":
                    done = payload
                elif event == "error":
                    errors.append(payload)
    except httpx.HTTPError as exc:
        raise SmokeError(f"流式请求失败：{exc}") from exc

    if errors:
        raise SmokeError(f"收到 error 事件：{errors[0]}")
    missing = {"citations", "token", "done"} - seen
    if missing:
        raise SmokeError(f"缺少 SSE 事件 {sorted(missing)}；实际收到 {sorted(seen)}")
    if not citations:
        print("  [WARN] citations 为空：回答可能是拒答（no_context / low_score）")

    return {
        "citations": citations,
        "token_count": token_count,
        "token_chars": token_chars,
        "done": done,
    }


def attempt(label: str, action: Callable[[], Any]) -> Any:
    """
    执行一步，打印 PASS / FAIL。

    :param label: 步骤名。
    :param action: 无参可调用。
    :return: ``action`` 的返回值。
    :raises SmokeAbort: ``action`` 抛 ``SmokeError``（原因已打印）。
    """
    try:
        value = action()
    except SmokeError as exc:
        print(f"[FAIL] {label}: {exc}")
        raise SmokeAbort from exc
    print(f"[PASS] {label}")
    return value


def main(argv: list[str] | None = None) -> int:
    """
    按顺序跑完冒烟步骤。

    :param argv: 命令行参数；``None`` 时读 ``sys.argv``。
    :return: 退出码（0 全绿，1 有失败）。
    """
    args = parse_args(argv)
    ingest_base = args.ingest_base.rstrip("/")
    chat_base = args.chat_base.rstrip("/")

    print("RAG demo smoke test")
    print(f"  ingest:  {ingest_base}")
    print(f"  chat:    {chat_base}")
    print(f"  timeout: {args.timeout:.0f}s")
    print()

    timeout = httpx.Timeout(args.timeout, connect=min(10.0, args.timeout))
    try:
        with (
            httpx.Client(timeout=timeout) as ingest_client,
            httpx.Client(timeout=timeout) as chat_client,
        ):
            attempt("ingest /health", lambda: check_health(ingest_client, ingest_base))
            attempt("chat /health", lambda: check_health(chat_client, chat_base))

            doc = attempt("读取 scripts/example-doc.json", load_example_doc)
            question = str(doc.get("title") or "").strip() or "部署手册"

            document_id: int | None = None
            if args.skip_ingest:
                print("[SKIP] 入库样例文档（--skip-ingest）")
                document_id = attempt(
                    "定位已有样例文档",
                    lambda: find_document_id(ingest_client, ingest_base, doc),
                )
                if document_id is None:
                    print("[SKIP] 等待 chunk 向量化：库里没有这篇样例")
                else:
                    print(f"  document id = {document_id}")
            else:
                document = attempt(
                    "入库样例文档",
                    lambda: ingest_document(ingest_client, ingest_base, doc),
                )
                document_id = int(document["id"])
                print(f"  document id = {document_id}")

            if document_id is not None:
                chunks = attempt(
                    "等待 chunk 向量化",
                    lambda: wait_for_chunks(
                        ingest_client, ingest_base, document_id, args.timeout
                    ),
                )
                print(f"  {len(chunks)} chunks done")

            results = attempt(
                "检索 /api/v1/search",
                lambda: search_chunks(chat_client, chat_base, question),
            )
            top = results[0]
            print(f"  命中 {len(results)} 条；第一条：{top.get('title')}（score={top.get('score')}）")

            stats = attempt(
                "流式问答 /api/v1/chat/stream",
                lambda: stream_chat(chat_client, chat_base, question),
            )
            done = stats["done"] or {}
            print(
                f"  citations={len(stats['citations'])} "
                f"token_events={stats['token_count']} "
                f"token_chars={stats['token_chars']}"
            )
            print(
                f"  done: model={done.get('model')} degraded={done.get('degraded')} "
                f"grounded={done.get('grounded')} cache_hit={done.get('cache_hit')}"
            )
    except SmokeAbort:
        print()
        print("RESULT: FAIL")
        return 1

    print()
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
