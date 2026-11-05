/**
 * config.js — 前端运行配置（零构建，原生 ES module）。
 *
 * ``chatBase`` 留空表示与页面同源：演示时聊天页由 ``rag-api``（8001）挂载，
 * ``/api/v1/chat/stream`` 与 ``/api/v1/feedback`` 都走同源，不需要 CORS。
 * 运维页由 ``rag-api`` 提供、数据来自 ``api``（8000），因此单独指向它。
 */
export const CONFIG = {
  chatBase: "",
  adminBase: "http://localhost:8000",
  defaultTopK: 5,
};
