/**
 * ui.js — DOM 小工具：元素创建、引用 chips、错误条、提示、数字/时间格式化。
 *
 * 约束：来自接口或模型的文本默认通过 ``textContent`` 写入；Markdown 路径由
 * ``chat.js`` 先经 ``DOMPurify`` 白名单清洗，再使用 ``innerHTML``。本模块不提供
 * 未清洗的 ``innerHTML`` 渲染入口。
 */

const SVG_NS = "http://www.w3.org/2000/svg";
const numberFormatter = new Intl.NumberFormat("zh-CN");
const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

let liveRegion = null;

/**
 * 创建元素。
 *
 * @param {string} tag 标签名。
 * @param {string} [className] class 名。
 * @param {string|number} [text] 文本内容（走 textContent）。
 * @returns {HTMLElement} 新元素。
 */
export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

/**
 * 清空节点子元素。
 *
 * @param {Element} node 目标节点。
 */
export function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

/* ---- 无障碍播报 -------------------------------------------------------- */

/**
 * 通过隐藏的 ``aria-live`` 区域播报状态变化（回答开始、完成、错误等）。
 * 同一条消息重复播报时先清空再写入，确保读屏器能感知。
 *
 * @param {string} message 播报文本。
 */
export function announce(message) {
  if (typeof document === "undefined" || !message) {
    return;
  }
  if (!liveRegion) {
    liveRegion = document.getElementById("sr-status");
    if (!liveRegion) {
      liveRegion = el("div", "visually-hidden");
      liveRegion.id = "sr-status";
      liveRegion.setAttribute("aria-live", "polite");
      liveRegion.setAttribute("aria-atomic", "true");
      document.body.append(liveRegion);
    }
  }
  liveRegion.textContent = "";
  window.setTimeout(() => {
    liveRegion.textContent = message;
  }, 30);
}

/* ---- 图标（内联 SVG，不用 emoji） -------------------------------------- */

function createSvg(viewBox = "0 0 24 24") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  return svg;
}

function pathNode(d) {
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", d);
  return path;
}

/**
 * 警告三角图标。
 *
 * @returns {SVGElement} 图标。
 */
export function warningIcon() {
  const svg = createSvg();
  svg.append(
    pathNode(
      "M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"
    ),
    pathNode("M12 9v4"),
    pathNode("M12 17h.01")
  );
  return svg;
}

/**
 * 信息圆图标。
 *
 * @returns {SVGElement} 图标。
 */
export function infoIcon() {
  const svg = createSvg();
  const circle = document.createElementNS(SVG_NS, "circle");
  circle.setAttribute("cx", "12");
  circle.setAttribute("cy", "12");
  circle.setAttribute("r", "9");
  svg.append(circle, pathNode("M12 16v-4"), pathNode("M12 8h.01"));
  return svg;
}

/**
 * 关闭图标。
 *
 * @returns {SVGElement} 图标。
 */
export function closeIcon() {
  const svg = createSvg();
  svg.append(pathNode("M18 6 6 18"), pathNode("M6 6l12 12"));
  return svg;
}

/**
 * 点赞 / 点踩图标（同一个手势，朝下时旋转 180°）。
 *
 * @param {"up"|"down"} direction 方向。
 * @returns {SVGElement} 图标。
 */
export function thumbIcon(direction) {
  const svg = createSvg();
  const group = document.createElementNS(SVG_NS, "g");
  if (direction === "down") {
    group.setAttribute("transform", "rotate(180 12 12)");
  }
  group.append(
    pathNode("M7 10v12"),
    pathNode(
      "M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"
    )
  );
  svg.append(group);
  return svg;
}

/* ---- 链接安全 ---------------------------------------------------------- */

/**
 * 只放行 http(s) 链接，挡掉 ``javascript:`` 之类的伪协议。
 *
 * @param {string|null|undefined} value 原始 URL。
 * @returns {string|null} 规范化后的 URL，不合法时为 null。
 */
export function safeHttpUrl(value) {
  if (!value || typeof value !== "string") {
    return null;
  }
  try {
    const url = new URL(value, window.location.href);
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

/* ---- 引用 chips -------------------------------------------------------- */

/**
 * 把 ``citations`` 事件里的数组渲染成 chips 列表。
 *
 * @param {Array<{n:number,title:string,source:string,url?:string|null}>} citations 引用列表。
 * @returns {HTMLUListElement} ``ul.citation-list``。
 */
export function createCitationChips(citations) {
  const list = el("ul", "citation-list");
  for (const citation of citations) {
    list.append(createCitationChip(citation));
  }
  return list;
}

function createCitationChip(citation) {
  const href = safeHttpUrl(citation?.url);
  const tag = href ? "a" : "span";
  const chip = el(tag, "citation-chip");
  const n = Number(citation?.n);
  const title = citation?.title || "未命名资料";
  const source = citation?.source || "unknown";

  if (href) {
    chip.href = href;
    chip.target = "_blank";
    chip.rel = "noopener noreferrer";
    chip.setAttribute("aria-label", `引用 ${n}：${title}（${source}），在新窗口打开原文`);
  } else {
    chip.classList.add("citation-chip--static");
    chip.setAttribute("aria-label", `引用 ${n}：${title}（${source}），无原文链接`);
  }

  chip.append(
    el("span", "citation-chip__n", Number.isFinite(n) ? n : "?"),
    el("span", "citation-chip__title", title),
    el("span", "citation-chip__meta", source)
  );
  if (href) {
    const arrow = el("span", "citation-chip__arrow", "↗");
    arrow.setAttribute("aria-hidden", "true");
    chip.append(arrow);
  }
  return chip;
}

/* ---- 错误条 / 提示 ----------------------------------------------------- */

/**
 * 创建可关闭的错误条。
 *
 * @param {{title?:string, detail?:string, onDismiss?:Function}} [options] 配置。
 * @returns {HTMLDivElement} ``.error-bar``（``role="alert"``）。
 */
export function createErrorBar({ title = "请求失败", detail = "", onDismiss } = {}) {
  const bar = el("div", "error-bar");
  bar.setAttribute("role", "alert");

  const icon = warningIcon();
  icon.classList.add("error-bar__icon");

  const body = el("div", "error-bar__body");
  body.append(el("p", "error-bar__title", title));
  if (detail) {
    body.append(el("p", "error-bar__detail", detail));
  }

  const dismiss = el("button", "error-bar__dismiss");
  dismiss.type = "button";
  dismiss.setAttribute("aria-label", "关闭错误提示");
  dismiss.append(closeIcon());
  dismiss.addEventListener("click", () => {
    bar.remove();
    if (typeof onDismiss === "function") {
      onDismiss();
    }
  });

  bar.append(icon, body, dismiss);
  return bar;
}

/**
 * 创建提示块（降级、部分失败等）。
 *
 * @param {string} message 提示文本。
 * @param {"info"|"warn"} [variant] 语义。
 * @returns {HTMLDivElement} ``.notice``。
 */
export function createNotice(message, variant = "info") {
  const notice = el("div", `notice notice--${variant}`);
  const icon = variant === "warn" ? warningIcon() : infoIcon();
  icon.classList.add("notice__icon");
  notice.append(icon, el("p", "", message));
  return notice;
}

/* ---- 徽标 / 指标 ------------------------------------------------------- */

/**
 * 创建徽标。
 *
 * @param {string} text 文本。
 * @param {"neutral"|"accent"|"ok"|"warn"|"danger"} [variant] 语义。
 * @param {{dot?:boolean}} [options] 是否显示状态点。
 * @returns {HTMLSpanElement} ``.badge``。
 */
export function createBadge(text, variant = "neutral", { dot = false } = {}) {
  const badge = el("span", `badge badge--${variant}`, text);
  if (dot) {
    const dotNode = el("span", "badge__dot");
    dotNode.setAttribute("aria-hidden", "true");
    badge.prepend(dotNode);
  }
  return badge;
}

/**
 * 创建一行“指标名 + 指标值”。
 *
 * @param {string} label 指标名。
 * @param {string|number} value 指标值（已格式化）。
 * @returns {HTMLSpanElement} ``.metric``。
 */
export function createMetric(label, value) {
  const metric = el("span", "metric");
  metric.append(el("span", "metric__k", label), el("span", "metric__v", value));
  return metric;
}

/**
 * chunk 状态到徽标语义的映射。
 *
 * @param {string} status 后端状态。
 * @returns {"neutral"|"accent"|"ok"|"warn"|"danger"} 徽标变体。
 */
export function statusVariant(status) {
  switch (String(status || "").toLowerCase()) {
    case "done":
      return "ok";
    case "pending":
      return "warn";
    case "processing":
      return "accent";
    case "failed":
      return "danger";
    default:
      return "neutral";
  }
}

const STATUS_LABELS = {
  done: "已完成",
  pending: "待处理",
  processing: "处理中",
  failed: "失败",
  skipped: "已跳过",
  retracted: "已作废",
};

/**
 * chunk 状态的中文标签。
 *
 * @param {string} status 后端状态。
 * @returns {string} 展示文本。
 */
export function statusLabel(status) {
  const key = String(status || "").toLowerCase();
  return STATUS_LABELS[key] || status || "未知";
}

/* ---- 格式化 ------------------------------------------------------------ */

/**
 * 格式化计数；非法值显示破折号，不伪造 0。
 *
 * @param {number|string|null|undefined} value 原始值。
 * @returns {string} 展示文本。
 */
export function formatNumber(value) {
  if (value === null || value === undefined || value === "") {
    return "—";
  }
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "—";
  }
  return numberFormatter.format(number);
}

/**
 * 格式化毫秒延迟。
 *
 * @param {number|null|undefined} value 毫秒。
 * @returns {string} 展示文本。
 */
export function formatMs(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return "—";
  }
  const ms = Number(value);
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  return `${(ms / 1000).toFixed(2)} s`;
}

/**
 * 格式化 token 数（k / M 缩写）。
 *
 * @param {number|null|undefined} value token 数。
 * @returns {string} 展示文本。
 */
export function formatTokens(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return "—";
  }
  const tokens = Number(value);
  if (tokens < 1000) {
    return String(tokens);
  }
  if (tokens < 1_000_000) {
    return `${(tokens / 1000).toFixed(tokens < 10_000 ? 1 : 0)}k`;
  }
  return `${(tokens / 1_000_000).toFixed(1)}M`;
}

/**
 * 格式化日期时间；空值显示破折号。
 *
 * @param {string|Date|null|undefined} value ISO 时间或 Date。
 * @returns {string} 本地时间文本。
 */
export function formatDateTime(value) {
  if (!value) {
    return "—";
  }
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return dateTimeFormatter.format(date);
}
