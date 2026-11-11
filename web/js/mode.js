/**
 * mode.js — 判断当前页面是不是访客演示区。
 *
 * Caddy 的访客区固定挂在 ``/demo/`` 下（反代时剥离前缀），站主区是根路径。
 * 页面脚本据此隐藏运维入口、锁定筛选项；根路径访问不受影响。
 *
 * 用路径而不是请求头判断：浏览器脚本看不到 Caddy 注入的请求头，路径是唯一
 * 稳定信号。访客即使手动改 URL 回到根路径，也会被 Caddy 的站主账号拦下。
 */
const path = window.location.pathname;

/** 当前是否为访客演示区。 */
export const IS_DEMO = path === "/demo" || path.startsWith("/demo/");

if (IS_DEMO) {
  document.documentElement.classList.add("is-demo");

  // 访客区右上角加“退出”：清 Cookie 后回登录页
  const nav = document.querySelector(".site-nav");
  if (nav) {
    const logout = document.createElement("a");
    logout.href = "/demo/api/v1/auth/logout";
    logout.textContent = "退出";
    logout.setAttribute("aria-label", "退出访客登录");
    nav.append(logout);
  }
}
