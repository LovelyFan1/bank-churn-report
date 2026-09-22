"""全局鉴权中间件 —— 默认拒绝（default-deny）。

**为什么用中间件，而不是给每个接口加 Depends**

我最初的选择是依赖注入（理由见 auth_deps 顶部：白名单会漏）。但实测发现
本项目有 8 个 router、约 40 个接口**全都没有鉴权依赖** —— 逐个加要改
8 个文件，既容易漏，也违背"不轻易改动既有业务代码"的要求。

改用中间件后，语义反而更安全：

    依赖注入 = 白名单思维（声明了才保护 → 漏一个就裸奔）
    本中间件 = **黑名单思维**（默认全部保护 → 漏一个只是"该公开的没公开"）

即失败方向是"功能不可用"（立刻被发现）而不是"数据泄露"（长期无人察觉）。
新增接口**自动**受保护，这正是我们想要的默认。

**免鉴权白名单（刻意保持极短）**

    /api/auth/policy        登录页需要知道是否要求双因素
    /api/auth/login         第一步登录
    /api/auth/login/totp    第二步登录
    /api/health             容器健康检查
    /docs /openapi.json     接口文档

⚠ 白名单是**精确匹配**（不是前缀包含），避免 `/api/auth/login-anything`
  这类路径被意外放行。唯一的例外是 /docs 与其静态资源，单独判断。
"""

from __future__ import annotations

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings
from app.services import auth_service as auth

logger = logging.getLogger(__name__)

# 精确匹配的免鉴权路径（全部为认证自身 + 探测端点）
_PUBLIC_EXACT = {
    "/api/auth/policy",
    "/api/auth/login",
    "/api/auth/login/totp",
    # 演示辅助：用**临时票据**自证身份（此时尚无正式令牌，必须免中间件鉴权；
    # 它自己在端点内校验票据 scope，且仅在 AUTH_DEMO_SHOW_TOTP=true 时可用）
    "/api/auth/demo/totp",
    "/api/health",
    "/",
}

# 免鉴权前缀（仅接口文档）
_PUBLIC_PREFIX = ("/docs", "/redoc", "/openapi.json", "/static")

# 完全不受管的对象：前端静态资源与浏览器预检等
_SKIP_METHODS = {"OPTIONS", "HEAD"}


class AuthMiddleware(BaseHTTPMiddleware):
    """校验 Bearer 令牌；通过则把身份挂到 request.state 供下游复用。"""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        # 鉴权被显式关闭 —— 记录一次告警（只在首次），但仍然明确放行，
        # 因为这是用户显式的调试选择，不是静默降级。
        if not settings.AUTH_ENABLED:
            if not AuthMiddleware._warned:
                logger.warning("auth: AUTH_ENABLED=false —— 所有接口免鉴权（仅限调试）")
                AuthMiddleware._warned = True
            return await call_next(request)

        if method in _SKIP_METHODS or path in _PUBLIC_EXACT \
                or path.startswith(_PUBLIC_PREFIX):
            return await call_next(request)

        # 只保护 API；前端页面路由由 Vue Router 处理
        if not path.startswith("/api/"):
            return await call_next(request)

        authz = request.headers.get("authorization") or ""

        # ── 服务间内部调用（Agent 工具层回调自身接口）─────────────
        # ⚠ 必须放在用户令牌校验之前，且**令牌 + 回环来源**双重校验。
        #   仅凭令牌可被重放；仅凭来源则容器内任意进程都能绕过鉴权。
        if authz.lower().startswith("bearer "):
            maybe = authz.split(" ", 1)[1].strip()
            client_host = request.client.host if request.client else ""
            if auth.is_internal_request(maybe, client_host):
                # 内部调用不对应具体行员；写操作另经 /confirm 由用户令牌触发
                request.state.auth_user = {
                    "username": "__internal__",
                    "display_name": "系统内部调用",
                    "role": "admin",
                    "id": 0,
                    "totp_enabled": 0,
                }
                request.state.ip = client_host
                return await call_next(request)

        if not authz.lower().startswith("bearer "):
            return _unauthorized("未登录或缺少令牌")

        token = authz.split(" ", 1)[1].strip()
        try:
            payload = auth.decode_token(token)
        except auth.TokenError as e:
            return _unauthorized(str(e))

        if payload.get("scope") != "full":
            return _unauthorized("令牌不可用于该操作，请完成登录")

        username = payload.get("sub") or ""
        if not username:
            return _unauthorized("令牌缺少用户标识")

        # ⚠ 查库校验"用户仍存在且未停用"。这是无状态 JWT 的必经代价：
        #   不查库则停用账号要等到令牌自然过期才生效 —— 对银行内部系统
        #   不可接受（离职当天必须立刻失效）。代价是一次主键查询（<1ms）。
        #   复用同一个 session：依赖注入的 get_db 会另开一个，故这里自查自关。
        from app.database import SessionLocal
        from app.models.user import User
        db = SessionLocal()
        try:
            u = db.query(User).filter(User.username == username).first()
            if u is None:
                return _unauthorized("用户不存在，请重新登录")
            if not u.is_active:
                return _forbidden("账号已停用，请联系管理员")
            # 挂到 request.state，供 current_user 复用（避免二次查库）
            request.state.auth_user = {
                "username": u.username,
                "display_name": u.display_name or u.username,
                "role": u.role,
                "id": u.id,
                "totp_enabled": bool(u.totp_enabled),
            }
        finally:
            db.close()

        # 客户端 IP 留痕（审计用）—— XFF 可伪造，仅作参考
        xff = request.headers.get("x-forwarded-for")
        request.state.ip = (xff.split(",")[0].strip() if xff
                            else (request.client.host if request.client else ""))

        return await call_next(request)

    _warned = False


def _unauthorized(detail: str) -> JSONResponse:
    # 统一 401 语义：前端据此跳登录页
    return JSONResponse(status_code=401, content={"detail": detail})


def _forbidden(detail: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": detail})
