"""
SSE（Server-Sent Events）格式化。

一个事件由 **若干行 + 一个空行** 组成，空行才是"本事件结束"的边界；
``event:`` 给事件名，``data:`` 给 JSON 负载。前端用 ``EventSource`` 风格解析时，
正是靠空行把字节流切成一条条事件 —— 少写这个空行，事件会一直攒着不触发。

JSON 用 ``ensure_ascii=False`` ：中文直接输出，curl / 日志里也看得懂。
"""
import json
from typing import Any


def format_sse(event: str, data: dict[str, Any]) -> str:
    """
    把事件名与负载格式化成一条 SSE 消息。

    :param event: 事件名（``citations`` / ``token`` / ``done`` / ``error``）。
    :param data: JSON 可序列化的负载。
    :return: 以空行结尾的 SSE 文本，可直接写进 ``StreamingResponse``。
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
