/**
 * admin.js — 运维页入口：5 秒轮询文档统计、爬虫统计、LLM 用量与最近文档。
 *
 * 任一接口失败都显示错误条并保留“不可用”状态，不用假数字填充。
 */

import { CONFIG } from "./config.js";
import { HttpError, getJSON } from "./api.js";
import {
  announce,
  clear,
  createBadge,
  createErrorBar,
  el,
  formatDateTime,
  formatNumber,
  safeHttpUrl,
  statusLabel,
  statusVariant,
} from "./ui.js";

const POLL_MS = 5000;
const CRAWLER_FIELDS = ["fetched", "not_modified", "pushed", "deleted", "errors"];

// 来源中文解释：原始 source 名保留（便于和数据库/日志对照），鼠标悬停给出说明。
const SOURCE_HINTS = {
  web: "真实爬虫（FastAPI 中文站）",
  feishu: "模拟同步器",
  confluence: "模拟同步器",
  yuque: "模拟同步器",
  bench: "压测数据",
};

const dom = {
  errorSlot: document.getElementById("admin-error-slot"),
  updated: document.getElementById("admin-updated"),
  refresh: document.getElementById("refresh-button"),
  kpiTotal: document.getElementById("kpi-total"),
  kpiTotalMeta: document.getElementById("kpi-total-meta"),
  kpiToday: document.getElementById("kpi-today"),
  kpiDedup: document.getElementById("kpi-dedup"),
  kpiPending: document.getElementById("kpi-pending"),
  kpiPendingMeta: document.getElementById("kpi-pending-meta"),
  kpiPendingCard: document.getElementById("kpi-pending-card"),
  sourceList: document.getElementById("source-list"),
  sourceNote: document.getElementById("source-note"),
  chunkList: document.getElementById("chunk-list"),
  chunkSummary: document.getElementById("chunk-summary"),
  crawlerLastRun: document.getElementById("crawler-last-run"),
  crawlerSeen: document.getElementById("crawler-seen"),
  crawlerCounters: document.getElementById("crawler-counters"),
  crawlerError: document.getElementById("crawler-error"),
  llmUsageBody: document.getElementById("llm-usage-body"),
  llmProviderBadge: document.getElementById("llm-provider-badge"),
  llmModelHint: document.getElementById("llm-model-hint"),
  llmPriceNote: document.getElementById("llm-price-note"),
  docsWrap: document.getElementById("docs-wrap"),
  docsEmpty: document.getElementById("docs-empty"),
  docsBody: document.getElementById("docs-body"),
};

const state = {
  loading: false,
  lastSuccessAt: null,
  errorSignature: "",
};

/* ---- 工具 -------------------------------------------------------------- */

function describeError(error) {
  if (error instanceof HttpError) {
    return error.status ? `HTTP ${error.status} · ${error.message}` : error.message;
  }
  return error?.message ? String(error.message) : String(error);
}

function clockText(date) {
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function setUpdated(text) {
  dom.updated.textContent = text;
}

function showRefreshError(failures) {
  clear(dom.errorSlot);
  if (failures.length === 0) {
    return;
  }
  const reasons = failures.map((item) =>
    item.replace(/^[^：]+：/, "").replace(/。$/, "")
  );
  const detail =
    failures.length > 1 && new Set(reasons).size === 1
      ? `${failures.length} 个接口均失败：${reasons[0]}`
      : failures.map((item) => item.replace(/。$/, "")).join("；");

  dom.errorSlot.append(
    createErrorBar({
      title: "部分运维数据刷新失败",
      detail,
    })
  );
}

/* ---- 文档统计 ---------------------------------------------------------- */

function statusCount(chunkStatus, wanted) {
  const row = chunkStatus.find(
    (item) => String(item?.status || "").toLowerCase() === wanted
  );
  return Number(row?.count) || 0;
}

function renderStats(stats) {
  const chunkStatus = Array.isArray(stats?.chunk_status) ? stats.chunk_status : [];
  const pending = statusCount(chunkStatus, "pending");
  const processing = statusCount(chunkStatus, "processing");
  const done = statusCount(chunkStatus, "done");

  dom.kpiTotal.textContent = formatNumber(stats?.total);
  const benchCount = Number(stats?.bench_count) || 0;
  dom.kpiTotalMeta.textContent =
    benchCount > 0
      ? `MySQL 文档表 · 含 bench 压测 ${formatNumber(benchCount)} 篇`
      : "MySQL 文档表";
  dom.kpiToday.textContent = formatNumber(stats?.today_count);
  dom.kpiDedup.textContent = formatNumber(stats?.dedup_rejected);
  dom.kpiPending.textContent = formatNumber(pending);
  dom.kpiPendingMeta.textContent = `processing ${formatNumber(
    processing
  )} · done ${formatNumber(done)}`;
  dom.kpiPendingCard.classList.toggle("stat-card--warn", pending > 0);

  renderSources(
    Array.isArray(stats?.by_source) ? stats.by_source : [],
    stats?.bench_count
  );
  renderChunks(chunkStatus, pending);
}

function renderSources(bySource, benchCount) {
  clear(dom.sourceList);
  dom.sourceNote.textContent = "";

  // 后端已经把 bench 从 by_source 拆出去（bench_count 单独返回）；前端仍防御性过滤，
  // 兼容还没重建的旧后端；旧 payload 里若还带 bench，用它兜底显示隐藏数量。
  const listedBench = bySource.find((item) => String(item?.source) === "bench");
  const bench = Number(benchCount) || Number(listedBench?.count) || 0;
  const visible = bySource.filter((item) => String(item?.source) !== "bench");
  if (bench > 0) {
    dom.sourceNote.textContent = `已隐藏 bench 压测数据 ${formatNumber(
      bench
    )} 篇；上方只展示真实/模拟来源。`;
  }

  if (visible.length === 0) {
    dom.sourceList.append(
      el("p", "empty-inline", "暂无实际来源数据（bench 已隐藏）")
    );
    return;
  }

  const max = Math.max(
    1,
    ...visible.map((item) => Number(item?.count) || 0)
  );

  for (const item of visible) {
    const count = Number(item?.count) || 0;
    const label = item?.source || "unknown";
    const row = el("div", "bar-row");
    const track = el("div", "bar-row__track");
    const fill = el("div", "bar-row__fill");
    fill.style.width = count > 0 ? `${Math.max(1, (count / max) * 100)}%` : "0%";
    fill.setAttribute("aria-hidden", "true");
    track.append(fill);
    row.setAttribute("aria-label", `${label}：${formatNumber(count)} 篇`);
    row.title = `${label}：${SOURCE_HINTS[label] || "未知来源"}`;
    row.append(
      el("span", "bar-row__label", label),
      track,
      el("span", "bar-row__count", formatNumber(count))
    );
    dom.sourceList.append(row);
  }
}

function renderChunks(chunkStatus, pending) {
  clear(dom.chunkList);
  dom.chunkSummary.textContent = "";

  if (chunkStatus.length === 0) {
    dom.chunkList.append(el("p", "empty-inline", "暂无 chunk 数据"));
    dom.chunkSummary.textContent = "文档入库并完成分块后，这里会显示状态分布。";
    return;
  }

  const order = ["pending", "processing", "done", "failed"];
  const sorted = [...chunkStatus].sort((a, b) => {
    const ai = order.indexOf(String(a?.status || "").toLowerCase());
    const bi = order.indexOf(String(b?.status || "").toLowerCase());
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  for (const item of sorted) {
    const status = String(item?.status || "").toLowerCase();
    const row = el("div", "status-row");
    const left = el("div", "status-row__left");
    left.append(createBadge(statusLabel(status), statusVariant(status), { dot: true }));
    row.append(left, el("span", "status-row__count", formatNumber(item?.count)));
    dom.chunkList.append(row);
  }

  const total = chunkStatus.reduce(
    (sum, item) => sum + (Number(item?.count) || 0),
    0
  );
  dom.chunkSummary.textContent = `共 ${formatNumber(total)} 个活跃 chunk · 待向量化 ${formatNumber(
    pending
  )} 个`;
}

function renderStatsFailure(error) {
  dom.kpiTotal.textContent = "—";
  dom.kpiTotalMeta.textContent = "MySQL 文档表";
  dom.kpiToday.textContent = "—";
  dom.kpiDedup.textContent = "—";
  dom.kpiPending.textContent = "—";
  dom.kpiPendingMeta.textContent = "数据不可用";
  dom.kpiPendingCard.classList.remove("stat-card--warn");

  clear(dom.sourceList);
  dom.sourceList.append(
    el("p", "empty-inline empty-inline--error", "来源分布暂时不可用")
  );
  dom.sourceNote.textContent = "";
  clear(dom.chunkList);
  dom.chunkList.append(
    el("p", "empty-inline empty-inline--error", "chunk 状态暂时不可用")
  );
  dom.chunkSummary.textContent = describeError(error);
}

/* ---- 爬虫统计 ---------------------------------------------------------- */

function renderCrawler(data) {
  clear(dom.crawlerError);
  dom.crawlerLastRun.textContent = formatDateTime(data?.last_run);
  dom.crawlerSeen.textContent = formatNumber(data?.seen);

  const stats = data?.stats && typeof data.stats === "object" ? data.stats : {};
  clear(dom.crawlerCounters);
  for (const field of CRAWLER_FIELDS) {
    const cell = el("div", `counter${field === "errors" ? " counter--danger" : ""}`);
    cell.append(
      el("span", "counter__label", field),
      el("span", "counter__value", formatNumber(stats[field]))
    );
    dom.crawlerCounters.append(cell);
  }
}

function renderCrawlerFailure(error) {
  dom.crawlerLastRun.textContent = "—";
  dom.crawlerSeen.textContent = "—";
  clear(dom.crawlerCounters);
  for (const field of CRAWLER_FIELDS) {
    const cell = el("div", `counter${field === "errors" ? " counter--danger" : ""}`);
    cell.append(
      el("span", "counter__label", field),
      el("span", "counter__value", "—")
    );
    dom.crawlerCounters.append(cell);
  }
  clear(dom.crawlerError);
  dom.crawlerError.append(
    el(
      "p",
      "empty-inline empty-inline--error crawler-error",
      `爬虫统计暂时不可用：${describeError(error)}`
    )
  );
}

/* ---- LLM 使用量 -------------------------------------------------------- */

const CURRENCY_SYMBOLS = { CNY: "¥", USD: "$" };

/**
 * 把 API 返回的币种代码转成展示符号。
 *
 * @param {string|undefined} currency 币种代码，如 CNY / USD。
 * @returns {string} 展示符号；未知币种回退为原代码。
 */
function currencySymbol(currency) {
  const code = String(currency || "").toUpperCase();
  return CURRENCY_SYMBOLS[code] || currency || "";
}

/**
 * 把估算费用转成展示字符串。
 *
 * @param {number|null|undefined} value 费用；null 表示单价未配置。
 * @param {string} currency 货币符号/代码。
 * @returns {string} 展示文本。
 */
function formatCost(value, currency, pricedCalls) {
  if (!Number(pricedCalls)) {
    return "未计价";
  }
  if (value === null || value === undefined) {
    return "未计价";
  }
  const amount = Number(value);
  if (!Number.isFinite(amount)) {
    return "—";
  }
  return `${currencySymbol(currency)}${amount.toFixed(4)}`.trim();
}

function appendLlmRow(label, today, total, { costRow = false } = {}) {
  const row = el(
    "tr",
    costRow ? "llm-table__row llm-table__row--cost" : "llm-table__row"
  );
  row.append(
    el("td", "llm-table__label", label),
    el("td", "llm-table__num", today),
    el("td", "llm-table__num", total)
  );
  dom.llmUsageBody.append(row);
}

function renderLlmUsage(data) {
  const today = data?.today || {};
  const total = data?.total || {};
  const currency = data?.currency || "";
  const pricingKnown = data?.price_input_cache_miss_per_1m !== null &&
    data?.price_input_cache_miss_per_1m !== undefined;

  dom.llmProviderBadge.textContent = `${data?.provider || "unknown"} · ${
    data?.model || "unknown"
  }`;
  dom.llmModelHint.textContent = "今日 / 累计";
  const symbol = currencySymbol(currency);
  dom.llmPriceNote.textContent = pricingKnown
    ? `DeepSeek 官方人民币价（${data?.pricing_retrieved_at || "—"}） · 当前时段 ${
        data?.pricing_period || "—"
      } · cache hit ${symbol}${data?.price_input_cache_hit_per_1m}/1M · miss ${symbol}${
        data?.price_input_cache_miss_per_1m
      }/1M · output ${symbol}${data?.price_output_per_1m}/1M；费用为估算值，节假日按工作日近似。`
    : "当前模型未在价格表中，仅显示 token，不估算费用。";

  clear(dom.llmUsageBody);
  appendLlmRow("请求数", formatNumber(today.requests), formatNumber(total.requests));
  appendLlmRow("LLM 调用", formatNumber(today.llm_calls), formatNumber(total.llm_calls));
  appendLlmRow(
    "检索片段模式",
    formatNumber(today.extractive_calls),
    formatNumber(total.extractive_calls)
  );
  appendLlmRow(
    "降级",
    formatNumber(today.degraded_calls),
    formatNumber(total.degraded_calls)
  );
  appendLlmRow(
    "缓存命中",
    formatNumber(today.cache_hits),
    formatNumber(total.cache_hits)
  );
  appendLlmRow("拒答", formatNumber(today.refusals), formatNumber(total.refusals));
  appendLlmRow(
    "已计价调用",
    formatNumber(today.priced_calls),
    formatNumber(total.priced_calls)
  );
  appendLlmRow(
    "Prompt tokens",
    formatNumber(today.prompt_tokens),
    formatNumber(total.prompt_tokens)
  );
  appendLlmRow(
    "Completion tokens",
    formatNumber(today.completion_tokens),
    formatNumber(total.completion_tokens)
  );
  appendLlmRow(
    "总 tokens",
    formatNumber(today.total_tokens),
    formatNumber(total.total_tokens)
  );
  appendLlmRow(
    "估算费用",
    formatCost(today.estimated_cost, currency, today.priced_calls),
    formatCost(total.estimated_cost, currency, total.priced_calls),
    { costRow: true }
  );
}

function renderLlmUsageFailure(error) {
  dom.llmProviderBadge.textContent = "—";
  dom.llmModelHint.textContent = "今日 / 累计";
  dom.llmPriceNote.textContent = `LLM 用量暂时不可用：${describeError(error)}`;
  clear(dom.llmUsageBody);
  const row = el("tr");
  const cell = el("td", "empty-inline empty-inline--error", "LLM 用量暂时不可用");
  cell.colSpan = 3;
  row.append(cell);
  dom.llmUsageBody.append(row);
}

/* ---- 最近文档 ---------------------------------------------------------- */

function createTitleCell(item) {
  const cell = el("td");
  const title = item?.title || "未命名文档";
  const href = safeHttpUrl(item?.url);
  if (href) {
    const link = el("a", "data-table__title", title);
    link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.title = title;
    cell.append(link);
  } else {
    const span = el("span", "data-table__title", title);
    span.title = title;
    cell.append(span);
  }
  return cell;
}

function renderDocuments(data) {
  const items = Array.isArray(data?.items) ? data.items : [];
  clear(dom.docsBody);

  if (items.length === 0) {
    dom.docsWrap.hidden = true;
    dom.docsEmpty.hidden = false;
    dom.docsEmpty.classList.remove("empty-inline--error");
    dom.docsEmpty.textContent = "暂无文档。可通过 POST /api/v1/documents 推入，或运行爬虫。";
    return;
  }

  for (const item of items) {
    const row = el("tr");
    row.append(
      el("td", "data-table__id", item?.id ?? "—"),
      createTitleCell(item),
      (() => {
        const cell = el("td");
        cell.append(createBadge(item?.source || "unknown", "neutral"));
        return cell;
      })(),
      (() => {
        const cell = el("td");
        cell.append(createBadge(item?.language || "—", "neutral"));
        return cell;
      })(),
      el("td", "numeric", formatNumber(item?.char_count)),
      el("td", "u-small u-muted", formatDateTime(item?.fetched_at))
    );
    dom.docsBody.append(row);
  }

  dom.docsWrap.hidden = false;
  dom.docsEmpty.hidden = true;
}

function renderDocumentsFailure(error) {
  clear(dom.docsBody);
  dom.docsWrap.hidden = true;
  dom.docsEmpty.hidden = false;
  dom.docsEmpty.classList.add("empty-inline--error");
  dom.docsEmpty.textContent = `最近文档暂时不可用：${describeError(error)}`;
}

/* ---- 轮询 -------------------------------------------------------------- */

async function loadAll() {
  if (state.loading || document.hidden) {
    return;
  }
  state.loading = true;
  dom.refresh.disabled = true;
  setUpdated("刷新中…");

  const [statsResult, crawlerResult, docsResult, llmResult] =
    await Promise.allSettled([
      getJSON(`${CONFIG.adminBase}/api/v1/documents/stats`),
      getJSON(`${CONFIG.adminBase}/api/v1/documents/crawler-stats`),
      getJSON(`${CONFIG.adminBase}/api/v1/documents?limit=10`),
      getJSON(`${CONFIG.adminBase}/api/v1/documents/llm-usage`),
    ]);

  const failures = [];

  if (statsResult.status === "fulfilled") {
    renderStats(statsResult.value);
  } else {
    renderStatsFailure(statsResult.reason);
    failures.push(`文档统计：${describeError(statsResult.reason)}`);
  }

  if (crawlerResult.status === "fulfilled") {
    renderCrawler(crawlerResult.value);
  } else {
    renderCrawlerFailure(crawlerResult.reason);
    failures.push(`爬虫统计：${describeError(crawlerResult.reason)}`);
  }

  if (docsResult.status === "fulfilled") {
    renderDocuments(docsResult.value);
  } else {
    renderDocumentsFailure(docsResult.reason);
    failures.push(`最近文档：${describeError(docsResult.reason)}`);
  }

  if (llmResult.status === "fulfilled") {
    renderLlmUsage(llmResult.value);
  } else {
    renderLlmUsageFailure(llmResult.reason);
    failures.push(`LLM 用量：${describeError(llmResult.reason)}`);
  }

  const signature = failures.join("|");
  if (signature !== state.errorSignature) {
    state.errorSignature = signature;
    showRefreshError(failures);
  }
  if (failures.length === 0) {
    state.lastSuccessAt = new Date();
    setUpdated(`更新于 ${clockText(state.lastSuccessAt)} · 每 5 秒轮询`);
  } else if (state.lastSuccessAt) {
    setUpdated(`刷新失败 · 上次成功 ${clockText(state.lastSuccessAt)}`);
  } else {
    setUpdated("刷新失败 · 尚无成功记录");
  }

  state.loading = false;
  dom.refresh.disabled = false;
}

function init() {
  dom.refresh.addEventListener("click", () => {
    announce("正在刷新运维数据。");
    void loadAll();
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      void loadAll();
    }
  });

  window.setInterval(() => {
    void loadAll();
  }, POLL_MS);

  setUpdated("等待首次刷新…");
  void loadAll();
}

init();
