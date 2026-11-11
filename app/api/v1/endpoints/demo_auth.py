"""
访客表单登录接口：登录、Caddy ``forward_auth`` 校验、退出。

路由挂在 ``/api/v1/auth``；公网 Caddy 把 ``/demo/api/v1/auth/*`` 剥掉 ``/demo`` 前缀后
反代到这里，``forward_auth`` 则直接调用 ``/api/v1/auth/check``。
"""
import logging
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services.demo_auth_service import (
    LoginLimiter,
    get_session_secret,
    load_accounts,
    sign_session,
    verify_password,
    verify_session,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SESSION_COOKIE = "rag_demo_session"
SESSION_PATH = "/demo"
LOGIN_PATH = "/demo/login"

_limiter = LoginLimiter(
    settings.demo_login_max_failures, settings.demo_login_window_seconds
)


class LoginRequest(BaseModel):
    """登录表单请求体。"""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class LoginResponse(BaseModel):
    """登录成功响应。"""

    ok: bool = True
    user: str


def _client_ip(request: Request) -> str:
    """
    取客户端 IP。

    Caddy 会带 ``X-Forwarded-For``；取第一个地址，避免把 Caddy 容器 IP 当成所有人。

    :param request: 当前请求。
    :return: 用于限流的 IP 字符串。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _session_secret() -> str:
    """返回当前配置的会话密钥（空值时由服务层生成并复用）。"""
    return get_session_secret(settings.demo_session_secret)


@router.post("/login", response_model=LoginResponse, summary="访客登录")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
) -> LoginResponse:
    """
    校验访客账号并设置 HttpOnly 会话 Cookie。

    :param payload: 用户名与密码。
    :param request: 用于读取客户端 IP。
    :param response: 用于写入 Cookie。
    :return: ``{"ok": true, "user": "..."}``。
    :raises HTTPException: 401 密码错误；429 尝试过多；503 账号文件不可读。
    """
    ip = _client_ip(request)
    limiter_key = f"{ip}:{payload.username}"
    if _limiter.is_blocked(limiter_key):
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试。")

    try:
        accounts = load_accounts(settings.demo_accounts_file)
    except OSError:
        logger.error("访客账号文件不可读：%s", settings.demo_accounts_file)
        raise HTTPException(
            status_code=503, detail="登录服务暂不可用，请稍后再试。"
        ) from None

    hashed = accounts.get(payload.username)
    if not hashed or not verify_password(payload.password, hashed):
        _limiter.record_failure(limiter_key)
        logger.warning("演示登录失败 user=%s ip=%s", payload.username, ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误。")

    _limiter.reset(limiter_key)
    token = sign_session(
        _session_secret(),
        payload.username,
        settings.demo_session_ttl_seconds,
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.demo_session_ttl_seconds,
        httponly=True,
        secure=settings.demo_cookie_secure,
        samesite="lax",
        path=SESSION_PATH,
    )
    logger.info("演示登录成功 user=%s ip=%s", payload.username, ip)
    return LoginResponse(user=payload.username)


@router.get("/check", summary="forward_auth 会话校验")
async def check(request: Request) -> Response:
    """
    校验 Cookie；给 Caddy ``forward_auth`` 用。

    有效时返回 200 + ``X-Demo-User``；否则 302 到登录页，Caddy 会把响应原样交给浏览器。

    :param request: 当前请求。
    :return: 200 或 302。
    """
    token = request.cookies.get(SESSION_COOKIE)
    username = verify_session(_session_secret(), token) if token else None
    if username:
        try:
            accounts = load_accounts(settings.demo_accounts_file)
        except OSError:
            accounts = {}
        if username in accounts:
            return Response(
                status_code=200,
                headers={"X-Demo-User": username, "Cache-Control": "no-store"},
            )

    next_path = request.headers.get("x-forwarded-uri", "/demo/")
    if not next_path.startswith("/demo/") or next_path.startswith(LOGIN_PATH):
        next_path = "/demo/"
    return RedirectResponse(
        f"{LOGIN_PATH}?next={quote(next_path, safe='')}",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/logout", summary="访客退出")
async def logout() -> Response:
    """
    清除会话 Cookie 并回登录页。

    :return: 303 重定向。
    """
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        path=SESSION_PATH,
        secure=settings.demo_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response
