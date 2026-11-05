/**
 * api.js — JSON 请求封装。
 *
 * 统一把「网络失败 / 非 2xx / 非 JSON」转成带 ``status`` 的 ``HttpError``，
 * 页面层只需要展示 ``error.message``，不用再各自判断状态码。
 */

/** 带 HTTP 状态码的请求错误。``status === 0`` 表示没拿到响应（网络层失败）。 */
export class HttpError extends Error {
  /**
   * @param {number} status HTTP 状态码，网络失败为 0。
   * @param {string} message 可直接展示的错误信息。
   * @param {string} [url] 出错的地址。
   */
  constructor(status, message, url = "") {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.url = url;
  }
}

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
      const messages = body.detail
        .map((item) => item?.msg || JSON.stringify(item))
        .join("；");
      return messages.slice(0, 300);
    }
    if (typeof body.message === "string") {
      return body.message.slice(0, 300);
    }
  } catch {
    // 不是 JSON：直接截断原文
  }
  return text.slice(0, 300);
}

async function request(method, url, payload, { signal, headers = {} } = {}) {
  const init = {
    method,
    headers: {
      Accept: "application/json",
      ...headers,
    },
    signal,
  };
  if (payload !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(payload);
  }

  let response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    if (error?.name === "AbortError") {
      throw error;
    }
    throw new HttpError(0, "网络请求失败，请确认后端已启动且地址可访问。", url);
  }

  if (!response.ok) {
    throw new HttpError(response.status, await readErrorDetail(response), url);
  }
  if (response.status === 204) {
    return null;
  }
  try {
    return await response.json();
  } catch {
    throw new HttpError(response.status, "响应不是合法 JSON。", url);
  }
}

/**
 * GET JSON。
 *
 * @param {string} url 地址。
 * @param {{signal?:AbortSignal}} [options] 取消信号。
 * @returns {Promise<any>} 解析后的 JSON。
 */
export function getJSON(url, options = {}) {
  return request("GET", url, undefined, options);
}

/**
 * POST JSON。
 *
 * @param {string} url 地址。
 * @param {any} payload 请求体（会被 JSON 序列化）。
 * @param {{signal?:AbortSignal}} [options] 取消信号。
 * @returns {Promise<any>} 解析后的 JSON。
 */
export function postJSON(url, payload, options = {}) {
  return request("POST", url, payload, options);
}
