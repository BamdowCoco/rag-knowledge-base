/**
 * sse.js — 用 ``fetch`` POST + ``ReadableStream`` 解析 SSE。
 *
 * 为什么不用 ``EventSource``：它只支持 GET，而 ``/api/v1/chat/stream`` 需要 POST
 * JSON 请求体。这里按 ``\n\n`` 缓冲切事件，兼容 CRLF、多行 ``data:``、注释行，
 * 并处理“一个 chunk 里多个事件 / 一个事件跨多个 chunk”两种情况。
 */

import { HttpError } from "./api.js";

const EVENT_SEPARATOR = /\r?\n\r?\n/;
const TEXT_DECODER = new TextDecoder();

async function readErrorDetail(response) {
  let text = "";
  try {
    text = await response.text();
  } catch {
    return response.statusText || `HTTP ${response.status}`;
  }
  if (!text) {
    return response.statusText || `HTTP ${response.status}`;
  }
  try {
    const body = JSON.parse(text);
    if (typeof body.detail === "string") {
      return body.detail.slice(0, 300);
    }
    if (Array.isArray(body.detail)) {
      return body.detail
        .map((item) => item?.msg || JSON.stringify(item))
        .join("；")
        .slice(0, 300);
    }
    if (typeof body.message === "string") {
      return body.message.slice(0, 300);
    }
  } catch {
    // 非 JSON，直接截断原文
  }
  return text.slice(0, 300);
}

/**
 * 解析单个 SSE 事件块。
 *
 * @param {string} block 事件块（不含结尾空行）。
 * @param {(event:string, data:any)=>Promise<void>|void} onEvent 回调。
 * @returns {Promise<void>} 处理完成。
 */
async function dispatchBlock(block, onEvent) {
  let eventName = "message";
  const dataLines = [];

  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) {
      continue;
    }
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) {
      value = value.slice(1);
    }
    if (field === "event") {
      eventName = value || "message";
    } else if (field === "data") {
      dataLines.push(value);
    }
    // id / retry 字段本项目用不到，忽略
  }

  if (dataLines.length === 0) {
    return;
  }

  const raw = dataLines.join("\n");
  if (!raw || raw === "[DONE]") {
    return;
  }

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch {
    await onEvent("parse_error", {
      code: "invalid_sse_json",
      message: "SSE data 不是合法 JSON。",
      raw: raw.slice(0, 200),
    });
    return;
  }
  await onEvent(eventName, payload);
}

/**
 * 发起 POST 并逐事件回调。
 *
 * @param {string} url SSE 地址。
 * @param {any} body POST 请求体（JSON 序列化）。
 * @param {(event:string, data:any)=>Promise<void>|void} onEvent 事件回调；
 *   事件名形如 ``citations`` / ``token`` / ``done`` / ``error``。
 * @param {AbortSignal} [signal] 取消信号。
 * @returns {Promise<void>} 流结束（或异常）时完成。
 */
export async function streamSSE(url, body, onEvent, signal) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    signal,
  }).catch((error) => {
    if (error?.name === "AbortError") {
      throw error;
    }
    throw new HttpError(0, "无法连接问答服务，请确认后端已启动。", url);
  });

  if (!response.ok) {
    throw new HttpError(response.status, await readErrorDetail(response), url);
  }
  if (!response.body) {
    throw new HttpError(response.status, "当前浏览器不支持流式响应。", url);
  }

  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("text/event-stream")) {
    const preview = (await response.text()).slice(0, 200);
    throw new HttpError(
      response.status,
      `响应不是 SSE（${contentType || "未知 Content-Type"}）：${preview}`,
      url
    );
  }

  const reader = response.body.getReader();
  let buffer = "";

  const drain = async (flush = false) => {
    let match = EVENT_SEPARATOR.exec(buffer);
    while (match) {
      const block = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      if (block.trim()) {
        await dispatchBlock(block, onEvent);
      }
      match = EVENT_SEPARATOR.exec(buffer);
    }
    if (flush && buffer.trim()) {
      await dispatchBlock(buffer, onEvent);
      buffer = "";
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += TEXT_DECODER.decode(value, { stream: true });
      await drain();
    }
    buffer += TEXT_DECODER.decode();
    await drain(true);
  } finally {
    reader.releaseLock();
  }
}
