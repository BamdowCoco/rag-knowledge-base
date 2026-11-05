/**
 * chat.js — 聊天页入口：筛选、SSE 流式回答、引用 chips、反馈、会话统计。
 *
 * 回答文本默认走 ``textContent`` 安全追加；启用 Markdown 时，HTML 必须先经
 * ``DOMPurify`` 白名单清洗。``question_hash`` 走 WebCrypto，不可用（非安全上下文）时省略。
 */

import { CONFIG } from "./config.js";
import { HttpError, postJSON } from "./api.js";
import { streamSSE } from "./sse.js";
import {
  announce,
  clear,
  createBadge,
  createCitationChips,
  createErrorBar,
  createMetric,
  createNotice,
  el,
  formatMs,
  formatNumber,
  formatTokens,
  safeHttpUrl,
  thumbIcon,
} from "./ui.js";

const MAX_QUESTION = 2000;
const MAX_TEXTAREA_HEIGHT = 168;
const MARKDOWN_RENDER_INTERVAL_MS = 80;
const MARKDOWN_ALLOWED_TAGS = [
  "p",
  "br",
  "strong",
  "em",
  "del",
  "code",
  "pre",
  "blockquote",
  "ul",
  "ol",
  "li",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "table",
  "thead",
  "tbody",
  "tr",
  "th",
  "td",
  "a",
  "hr",
];

const dom = {
  form: document.getElementById("ask-form"),
  input: document.getElementById("question"),
  count: document.getElementById("question-count"),
  send: document.getElementById("send-button"),
  stop: document.getElementById("stop-button"),
  clear: document.getElementById("clear-button"),
  conversation: document.getElementById("conversation"),
  empty: document.getElementById("empty-state"),
  errorSlot: document.getElementById("error-slot"),
  source: document.getElementById("filter-source"),
  language: document.getElementById("filter-language"),
  topk: document.getElementById("filter-topk"),
  railAnswers: document.getElementById("rail-answers"),
  railFirstToken: document.getElementById("rail-first-token"),
  railDegraded: document.getElementById("rail-degraded"),
  railCitations: document.getElementById("rail-citations"),
};

const state = {
  busy: false,
  controller: null,
  sessionId: makeSessionId(),
  autoFollow: true,
  stats: {
    answers: 0,
    firstTokenSum: 0,
    firstTokenCount: 0,
    degraded: 0,
    citations: 0,
  },
};

/* ---- 小工具 ------------------------------------------------------------ */

function makeSessionId() {
  try {
    if (globalThis.crypto?.randomUUID) {
      return globalThis.crypto.randomUUID();
    }
  } catch {
    // 非安全上下文时回退
  }
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function makeAnswerId() {
  try {
    if (globalThis.crypto?.randomUUID) {
      return globalThis.crypto.randomUUID();
    }
  } catch {
    // 忽略：省略 answer_id 即可
  }
  return null;
}

/**
 * 计算问题的 SHA-256，取前 12 位（与后端日志口径一致）。
 * 失败时返回 null，反馈请求里省略该字段。
 *
 * @param {string} question 问题原文。
 * @returns {Promise<string|null>} 十六进制摘要。
 */
async function hashQuestion(question) {
  try {
    if (!globalThis.crypto?.subtle) {
      return null;
    }
    const bytes = new TextEncoder().encode(question);
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(digest), (byte) =>
      byte.toString(16).padStart(2, "0")
    )
      .join("")
      .slice(0, 12);
  } catch {
    return null;
  }
}

function describeError(error) {
  if (error instanceof HttpError) {
    return error.status ? `HTTP ${error.status} · ${error.message}` : error.message;
  }
  return error?.message ? String(error.message) : String(error);
}

function showError(title, detail) {
  clear(dom.errorSlot);
  dom.errorSlot.append(createErrorBar({ title, detail }));
}

function isNearBottom() {
  const doc = document.documentElement;
  return window.innerHeight + window.scrollY >= doc.scrollHeight - 180;
}

let scrollQueued = false;
function follow() {
  if (!state.autoFollow || scrollQueued) {
    return;
  }
  scrollQueued = true;
  window.requestAnimationFrame(() => {
    scrollQueued = false;
    window.scrollTo(0, document.documentElement.scrollHeight);
  });
}

/* ---- 输入框 ------------------------------------------------------------ */

function resizeInput() {
  dom.input.style.height = "auto";
  const next = Math.min(dom.input.scrollHeight, MAX_TEXTAREA_HEIGHT);
  dom.input.style.height = `${next}px`;
}

function updateInputMeta() {
  dom.count.textContent = `${dom.input.value.length} / ${MAX_QUESTION}`;
  resizeInput();
}

/* ---- 筛选 -------------------------------------------------------------- */

function readFilters() {
  const filters = {};
  if (dom.source.value !== "all") {
    filters.source = [dom.source.value];
  }
  if (dom.language.value !== "all") {
    filters.language = dom.language.value;
  }
  return Object.keys(filters).length > 0 ? filters : null;
}

/* ---- 消息渲染 ---------------------------------------------------------- */

function appendUserMessage(question) {
  const article = el("article", "msg msg--user");
  article.append(el("div", "msg__meta", "你"), el("p", "msg__user-text", question));
  dom.conversation.append(article);
  return article;
}

function createAssistantMessage() {
  const article = el("article", "msg msg--assistant");
  article.setAttribute("aria-busy", "true");

  const meta = el("div", "msg__meta");
  const role = el("span", "msg__role", "助手");
  const model = el("span", "msg__model");
  model.hidden = true;
  const stateEl = el("span", "msg__state", "检索中…");
  meta.append(role, model, stateEl);

  const citations = el("div", "citations");
  citations.hidden = true;
  const citationsBody = el("div");
  citations.append(el("div", "citations__label", "引用"), citationsBody);

  const answer = el("div", "answer-text");
  answer.setAttribute("aria-live", "polite");

  const notices = el("div", "msg__notices");
  notices.hidden = true;

  const doneMeta = el("div", "msg__meta msg__meta--done");
  doneMeta.hidden = true;

  const tools = el("div", "msg__tools");
  tools.hidden = true;

  article.append(meta, citations, answer, notices, doneMeta, tools);
  dom.conversation.append(article);

  return {
    article,
    model,
    stateEl,
    citations,
    citationsBody,
    answer,
    raw: "",
    renderTimer: null,
    notices,
    doneMeta,
    tools,
  };
}

/**
 * 把原始 Markdown 渲染成安全的 HTML。
 *
 * - ``marked`` 负责 Markdown → HTML；
 * - ``DOMPurify`` 用白名单清洗，挡住 LLM 输出里的脚本/事件属性/伪协议；
 * - 链接只放行 http(s)，并强制 ``noopener noreferrer``；
 * - 任一库缺失时退回 ``textContent``，不至于整个聊天页不可用。
 *
 * @param {object} view 助手消息视图。
 * @param {string} raw 原始 Markdown 文本。
 * @param {boolean} streaming 是否仍在流式生成。
 */
function renderMarkdown(view, raw, streaming) {
  const text = raw || "";
  if (!text) {
    view.answer.textContent = "";
    return;
  }

  const markedLib = globalThis.marked;
  const purifyLib = globalThis.DOMPurify;
  if (!markedLib || !purifyLib) {
    view.answer.textContent = text;
    return;
  }

  let html;
  try {
    html = markedLib.parse(text, { gfm: true, breaks: true });
  } catch {
    view.answer.textContent = text;
    return;
  }

  const clean = purifyLib.sanitize(html, {
    ALLOWED_TAGS: MARKDOWN_ALLOWED_TAGS,
    ALLOWED_ATTR: ["href", "title", "class"],
  });
  if (!clean) {
    view.answer.textContent = text;
    return;
  }

  view.answer.innerHTML = clean;
  view.answer.classList.add("answer-text--md");
  view.answer.classList.toggle("is-streaming", Boolean(streaming));

  // DOMPurify 只保证了标签/属性安全；链接协议再按业务规则收一次口。
  for (const link of view.answer.querySelectorAll("a")) {
    const href = safeHttpUrl(link.getAttribute("href"));
    if (!href) {
      link.replaceWith(document.createTextNode(link.textContent || ""));
      continue;
    }
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
}

/**
 * 流式期间节流渲染：token 可能很密，80ms 合并一次，避免每字重排。
 *
 * @param {object} view 助手消息视图。
 */
function scheduleMarkdownRender(view) {
  if (view.renderTimer !== null) {
    return;
  }
  view.renderTimer = window.setTimeout(() => {
    view.renderTimer = null;
    renderMarkdown(view, view.raw, true);
    follow();
  }, MARKDOWN_RENDER_INTERVAL_MS);
}

/**
 * 立即渲染并取消待执行的节流任务（done / error / stop 时用）。
 *
 * @param {object} view 助手消息视图。
 * @param {boolean} [streaming] 是否保留流式光标。
 */
function flushMarkdown(view, streaming = false) {
  if (view.renderTimer !== null) {
    window.clearTimeout(view.renderTimer);
    view.renderTimer = null;
  }
  renderMarkdown(view, view.raw, streaming);
}

function renderDoneMeta(view, done) {
  const meta = view.doneMeta;
  clear(meta);
  const parts = [];
  const latency = done?.latency_ms || {};

  if (latency.first_token !== undefined) {
    parts.push(createMetric("首 token", formatMs(latency.first_token)));
  }
  if (latency.total !== undefined) {
    parts.push(createMetric("总耗时", formatMs(latency.total)));
  }
  if (latency.retrieval !== undefined) {
    parts.push(createMetric("检索", formatMs(latency.retrieval)));
  }
  if (done?.usage?.total_tokens !== undefined) {
    parts.push(createMetric("tokens", formatTokens(done.usage.total_tokens)));
  }
  if (
    done?.usage?.prompt_tokens !== undefined &&
    done?.usage?.completion_tokens !== undefined
  ) {
    parts.push(
      createMetric(
        "prompt / completion",
        `${formatTokens(done.usage.prompt_tokens)} / ${formatTokens(
          done.usage.completion_tokens
        )}`
      )
    );
  }
  if (done?.estimated_cost !== null && done?.estimated_cost !== undefined) {
    const costCode = String(done.cost_currency || "").toUpperCase();
    const currencySymbol =
      costCode === "CNY" ? "¥" : costCode === "USD" ? "$" : done.cost_currency ? `${done.cost_currency} ` : "";
    parts.push(
      createMetric("费用", `${currencySymbol}${Number(done.estimated_cost).toFixed(4)}`)
    );
  }
  if (done?.retrieval_mode) {
    const mode = done.score_type
      ? `${done.retrieval_mode} / ${done.score_type}`
      : done.retrieval_mode;
    parts.push(createMetric("模式", mode));
  }
  if (Number(done?.invalid_citations) > 0) {
    parts.push(createMetric("越界引用", formatNumber(done.invalid_citations)));
  }
  if (done?.grounded === false) {
    parts.push(createBadge("未接地", "warn", { dot: true }));
  }
  if (done?.cache_hit) {
    parts.push(createBadge("缓存命中", "accent", { dot: true }));
  }

  meta.append(...parts);
  meta.hidden = parts.length === 0;
}

function createRatingButton(rating, label) {
  const button = el("button", "icon-btn");
  button.type = "button";
  button.dataset.rating = rating;
  button.setAttribute("aria-label", label);
  button.setAttribute("aria-pressed", "false");
  button.append(thumbIcon(rating));
  return button;
}

function attachFeedback(view, questionHash, answerId) {
  const wrap = el("div", "feedback");
  const label = el("span", "feedback__label", "反馈");
  const up = createRatingButton("up", "回答有帮助");
  const down = createRatingButton("down", "回答没帮助");
  const result = el("span", "feedback__result");

  const submit = async (rating) => {
    if (wrap.dataset.done === "true") {
      return;
    }
    up.disabled = true;
    down.disabled = true;
    try {
      const payload = { rating };
      if (questionHash) {
        payload.question_hash = questionHash;
      }
      if (answerId) {
        payload.answer_id = answerId;
      }
      await postJSON(`${CONFIG.chatBase}/api/v1/feedback`, payload);
      const chosen = rating === "up" ? up : down;
      chosen.setAttribute("aria-pressed", "true");
      result.textContent = rating === "up" ? "已记录：有帮助" : "已记录：没帮助";
      wrap.dataset.done = "true";
      announce(result.textContent);
    } catch (error) {
      up.disabled = false;
      down.disabled = false;
      showError("反馈提交失败", describeError(error));
    }
  };

  up.addEventListener("click", () => submit("up"));
  down.addEventListener("click", () => submit("down"));

  wrap.append(label, up, down, result);
  view.tools.append(
    wrap,
    el("span", "msg__tools-hint", "反馈只记录评分与哈希，不保存问题原文")
  );
  view.tools.hidden = false;
}

function finalizeAnswer(view, done, citationCount, questionHash, answerId) {
  flushMarkdown(view, false);
  view.answer.classList.remove("is-streaming");
  view.article.setAttribute("aria-busy", "false");
  view.stateEl.textContent = "已完成";
  view.stateEl.classList.remove("msg__state--warn");

  if (done?.model) {
    view.model.textContent = `· ${done.model}`;
    view.model.hidden = false;
  }

  if (!view.answer.textContent.trim()) {
    view.answer.textContent = "（本次没有返回文字内容。）";
  }

  if (done?.degraded) {
    view.notices.append(
      createNotice("已降级为检索片段模式（模型不可用）。", "warn")
    );
    view.notices.hidden = false;
  }

  if (citationCount === 0) {
    clear(view.citationsBody);
    let text = "本次回答未附带引用。";
    if (done?.reason === "no_context") {
      text = "未检索到可引用资料。";
    } else if (done?.reason === "low_score") {
      text = "检索分数低于阈值，本次未使用资料。";
    }
    view.citationsBody.append(el("p", "u-small u-muted", text));
    view.citations.hidden = false;
  }

  renderDoneMeta(view, done);
  attachFeedback(view, questionHash, answerId);

  const citationText = citationCount > 0 ? `，引用 ${citationCount} 条` : "";
  announce(`回答完成，共 ${view.answer.textContent.length} 字${citationText}。`);
}

function handleStreamError(view, data) {
  const code = data?.code || "stream_error";
  const message = data?.message || "未知错误";
  flushMarkdown(view, false);
  view.answer.classList.remove("is-streaming");
  view.article.setAttribute("aria-busy", "false");
  view.stateEl.textContent = "生成中断";
  view.stateEl.classList.add("msg__state--warn");

  view.notices.append(createNotice(`生成中断：${message}（${code}）`, "warn"));
  if (data?.partial) {
    view.notices.append(createNotice("已输出的内容可能不完整。", "warn"));
  } else if (!view.answer.textContent.trim()) {
    view.answer.textContent = "（没有收到回答。）";
  }
  view.notices.hidden = false;

  showError("回答生成中断", `${code}：${message}`);
  announce("回答生成中断，请查看错误提示。");
}

/* ---- 会话侧栏 ---------------------------------------------------------- */

function renderRail() {
  const { stats } = state;
  const hasAnswer = stats.answers > 0;
  dom.railAnswers.textContent = hasAnswer ? formatNumber(stats.answers) : "—";
  dom.railFirstToken.textContent = stats.firstTokenCount
    ? formatMs(stats.firstTokenSum / stats.firstTokenCount)
    : "—";
  dom.railDegraded.textContent = hasAnswer ? formatNumber(stats.degraded) : "—";
  dom.railCitations.textContent = hasAnswer ? formatNumber(stats.citations) : "—";
}

function updateStats(done, citationCount) {
  const { stats } = state;
  stats.answers += 1;
  const firstToken = done?.latency_ms?.first_token;
  if (Number.isFinite(Number(firstToken))) {
    stats.firstTokenSum += Number(firstToken);
    stats.firstTokenCount += 1;
  }
  if (done?.degraded) {
    stats.degraded += 1;
  }
  stats.citations += citationCount;
  renderRail();
}

/* ---- 发送流程 ---------------------------------------------------------- */

function setBusy(busy) {
  state.busy = busy;
  dom.send.disabled = busy;
  dom.stop.hidden = !busy;
  dom.clear.disabled = busy;
  dom.form.setAttribute("aria-busy", busy ? "true" : "false");
}

async function ask(question) {
  setBusy(true);
  state.autoFollow = isNearBottom();
  clear(dom.errorSlot);
  if (dom.empty) {
    dom.empty.hidden = true;
  }

  appendUserMessage(question);
  const view = createAssistantMessage();
  dom.input.value = "";
  updateInputMeta();
  announce("正在检索相关资料。");
  follow();

  const controller = new AbortController();
  state.controller = controller;
  const answerId = makeAnswerId();
  let questionHash = null;
  let sawDone = false;
  let sawError = false;
  let citationCount = 0;

  try {
    questionHash = await hashQuestion(question);
    const filters = readFilters();
    const body = {
      question,
      top_k: Number(dom.topk.value) || CONFIG.defaultTopK,
      session_id: state.sessionId,
    };
    if (filters) {
      body.filters = filters;
    }

    await streamSSE(
      `${CONFIG.chatBase}/api/v1/chat/stream`,
      body,
      async (event, data) => {
        if (event === "citations") {
          const citations = Array.isArray(data?.citations) ? data.citations : [];
          citationCount = citations.length;
          if (citations.length > 0) {
            clear(view.citationsBody);
            view.citationsBody.append(createCitationChips(citations));
            view.citations.hidden = false;
            view.stateEl.textContent = "生成中…";
            announce(`收到 ${citations.length} 条引用，正在生成回答。`);
          }
          return;
        }

        if (event === "token") {
          if (typeof data?.text !== "string" || data.text === "") {
            return;
          }
          view.stateEl.textContent = "生成中…";
          view.answer.classList.add("is-streaming");
          view.raw += data.text;
          scheduleMarkdownRender(view);
          return;
        }

        if (event === "done") {
          sawDone = true;
          finalizeAnswer(view, data, citationCount, questionHash, answerId);
          updateStats(data, citationCount);
          return;
        }

        if (event === "error") {
          sawError = true;
          handleStreamError(view, data);
          return;
        }

        if (event === "parse_error") {
          sawError = true;
          showError("流式数据解析失败", data?.message || "收到无法解析的事件。");
        }
      },
      controller.signal
    );

    if (!sawDone && !sawError && !controller.signal.aborted) {
      flushMarkdown(view, false);
      view.answer.classList.remove("is-streaming");
      view.article.setAttribute("aria-busy", "false");
      view.stateEl.textContent = "连接已结束";
      view.stateEl.classList.add("msg__state--warn");
      showError("流式连接提前结束", "没有收到 done 事件，回答可能不完整。");
      announce("流式连接提前结束。");
    }
  } catch (error) {
    if (error?.name === "AbortError") {
      if (!sawDone) {
        flushMarkdown(view, false);
        view.answer.classList.remove("is-streaming");
        view.article.setAttribute("aria-busy", "false");
        view.stateEl.textContent = "已停止";
        if (!view.answer.textContent.trim()) {
          view.answer.textContent = "（已停止生成。）";
        }
        view.notices.append(createNotice("已停止生成。", "info"));
        view.notices.hidden = false;
        announce("已停止生成。");
      }
      return;
    }

    flushMarkdown(view, false);
    view.answer.classList.remove("is-streaming");
    view.article.setAttribute("aria-busy", "false");
    view.stateEl.textContent = "请求失败";
    view.stateEl.classList.add("msg__state--warn");
    if (!view.answer.textContent.trim()) {
      view.answer.textContent = "（没有收到回答。）";
    }
    const message = describeError(error);
    view.notices.append(createNotice(message, "warn"));
    view.notices.hidden = false;
    showError("问答请求失败", message);
    announce(`问答请求失败。${message}`);
  } finally {
    state.controller = null;
    setBusy(false);
    follow();
  }
}

/* ---- 事件绑定 ---------------------------------------------------------- */

function handleSubmit(event) {
  event.preventDefault();
  if (state.busy) {
    return;
  }
  const question = dom.input.value.trim();
  if (!question) {
    announce("请先输入问题。");
    dom.input.focus();
    return;
  }
  void ask(question);
}

function clearConversation() {
  if (state.busy) {
    return;
  }
  for (const child of Array.from(dom.conversation.children)) {
    if (child !== dom.empty) {
      child.remove();
    }
  }
  if (dom.empty) {
    dom.empty.hidden = false;
  }
  clear(dom.errorSlot);
  state.stats = {
    answers: 0,
    firstTokenSum: 0,
    firstTokenCount: 0,
    degraded: 0,
    citations: 0,
  };
  state.sessionId = makeSessionId();
  renderRail();
  announce("对话已清空。");
  dom.input.focus();
}

function init() {
  if (dom.topk) {
    dom.topk.value = String(CONFIG.defaultTopK);
  }

  dom.form.addEventListener("submit", handleSubmit);
  dom.input.addEventListener("input", updateInputMeta);
  dom.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      dom.form.requestSubmit();
    }
  });

  dom.stop.addEventListener("click", () => {
    if (state.controller) {
      announce("正在停止生成。");
      state.controller.abort();
    }
  });

  dom.clear.addEventListener("click", clearConversation);

  document.querySelectorAll("[data-question]").forEach((button) => {
    button.addEventListener("click", () => {
      dom.input.value = button.dataset.question || "";
      updateInputMeta();
      dom.input.focus();
      announce("示例问题已填入输入框。");
    });
  });

  window.addEventListener(
    "scroll",
    () => {
      state.autoFollow = isNearBottom();
    },
    { passive: true }
  );

  updateInputMeta();
  renderRail();
}

init();
