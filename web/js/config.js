/**
 * config.js — 前端运行配置（零构建，原生 ES module）。
 *
 * ``chatBase`` 留空表示与页面同源：演示时聊天页由 ``rag-api``（8001）挂载，
 * ``/api/v1/chat/stream`` 与 ``/api/v1/feedback`` 都走同源，不需要 CORS。
 *
 * 访客区页面在 ``/demo/`` 下，接口前缀也走 ``/demo``：Caddy 剥离前缀后
 * 反代到 ``rag-api``，并注入 ``X-Demo-Mode: 1``。
 *
 * ``adminBase``：
 * - 本地直接开 ``http://localhost:8001`` 时，运维页数据来自 api（8000），需要跨源；
 * - 经 Caddy / 公网访问时，页面与 API 同源（Caddy 把 documents/chunks 路由到 api），
 *   留空即可，避免浏览器去访问用户自己的 localhost:8000。
 */
import { IS_DEMO } from "./mode.js";

const localHosts = new Set(["localhost", "127.0.0.1"]);
const directRagPage =
  localHosts.has(window.location.hostname) && window.location.port === "8001";

export const CONFIG = {
  chatBase: IS_DEMO ? "/demo" : "",
  adminBase: directRagPage ? "http://localhost:8000" : "",
  defaultTopK: 5,
  demoMode: IS_DEMO,
};
