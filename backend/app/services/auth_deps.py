"""认证依赖与审计辅助 —— 供各 router 用 `Depends` 取当前登录人。

**为什么用依赖注入而不是中间件做鉴权**

中间件拦所有请求需要维护一份"哪些路径免鉴权"的白名单 —— 而白名单
会随新接口增加而忘记更新（漏一个就是未授权可访问）。依赖注入把
「这个接口需要登录」写在**接口定义旁边**，看得见、不会漏，
且能直接声明所需权限（`require_perm`）。

⚠ 但**审计日志不能靠依赖注入**：依赖只在被声明处生效，
  写操作散落在多个 router 里，靠自觉声明必然漏。故审计走
  显式的 `write_audit()` 调用 + 关键路径的集中处理（见 work_orders
  与 agent router 的接入点）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import AuditLog, User
from app.services import auth_service as auth

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """取真实客户端 IP —— 优先信任反代的 X-Forwarded-For 首项。

    ⚠ 仅用于审计留痕，不作为任何授权判据 —— XFF 可被伪造。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """取当前登录人。

    ⚠ 优先复用 `AuthMiddleware` 已经验好的结果（挂在 request.state.auth_user），
      避免每个请求查两次库。若中间件未启用（例如单元测试直接调 router），
      则回落到自己解析令牌 —— 两条路径的**校验强度必须一致**，
      否则"绕过中间件"就成了漏洞。
    """
    if settings_allow_anonymous():
        # 鉴权被显式关闭（仅用于本地调试），返回一个虚拟管理员。
        # ⚠ 判断方向必须是「allow_anonymous 为真才放行」。早先写成
        #   `if not settings_allow_anonymous(): return _anonymous_admin(db)`
        #   —— 把条件写反，导致 **AUTH_ENABLED=true 时全部请求都被当成
        #   匿名管理员放行**，等于鉴权完全失效（实测 12 项断言失败才暴露）。
        #   这类"开关反了"的缺陷不会有任何报错，只会安静地不设防。
        return _anonymous_admin(db)

    cached = getattr(request.state, "auth_user", None)
    if cached:
        u = User()
        u.id = cached.get("id") or 0
        u.username = cached.get("username") or ""
        u.display_name = cached.get("display_name") or ""
        u.role = cached.get("role") or ""
        u.is_active = 1
        u.totp_enabled = 1 if cached.get("totp_enabled") else 0
        u.totp_secret = None
        u.failed_attempts = 0
        # ⚠ 不查库即返回，因此 **不能**把该对象用于写操作（改口令/计数）。
        #   需要写 users 表的地方（如 change_password）必须自行重新查库。
        return u

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录或缺少令牌")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = auth.decode_token(token)
    except auth.TokenError as e:
        raise HTTPException(status_code=401, detail=str(e))

    # ⚠ 只有 scope=full 的令牌才能访问业务接口。登录第一步签发的
    #   `pre_totp` 临时票据若被接受，等于第二因子形同虚设。
    if payload.get("scope") != "full":
        raise HTTPException(status_code=401, detail="令牌不可用于该操作，请完成登录")

    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="令牌缺少用户标识")

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在，请重新登录")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")
    return user


def settings_allow_anonymous() -> bool:
    from app.config import settings
    return not settings.AUTH_ENABLED


def _anonymous_admin(db: Session) -> User:
    """AUTH_ENABLED=false 时的虚拟身份。

    ⚠ 这不是"降级放行"，而是**显式的调试开关**：默认开启鉴权，
      要关必须显式设 AUTH_ENABLED=false，且会在启动日志与 /api/auth/me
      里如实反映（前端会显示"鉴权已关闭"警示）。
    """
    u = User()
    u.id = 0
    u.username = "anonymous"
    u.display_name = "（鉴权已关闭）"
    u.department = "-"
    u.role = "admin"
    u.is_active = 1
    u.totp_enabled = 0
    return u


def require_perm(perm: str):
    """生成一个校验指定权限的依赖。

    用法：`user: User = Depends(require_perm(auth.PERM_ORDER_WRITE))`
    """
    def _dep(user: User = Depends(current_user)) -> User:
        if not auth.has_perm(user.role, perm):
            raise HTTPException(
                status_code=403,
                detail=f"当前角色（{auth.ROLE_LABELS.get(user.role, user.role)}）"
                       f"无权执行此操作",
            )
        return user
    return _dep


# ══════════════════════════════════════════════════════════
# 审计
# ══════════════════════════════════════════════════════════

def write_audit(db: Session, *, user: User | None, action: str,
                target: str = "", detail: Any = None, source: str = "ui",
                success: bool = True, message: str = "",
                ip: str = "", commit: bool = False) -> None:
    """写一条审计记录。

    ⚠ 审计写入**不得**导致业务操作失败：若审计表出问题（如磁盘满），
      让一次已经成功的建单回滚是难以接受的 —— 用户看到"操作失败"，
      但底层可能已经发过通知。故此处吞掉异常并记日志。

    ⚠ 为什么用 **savepoint（begin_nested）** 而不是直接 rollback：
      直接 `db.rollback()` 会回滚**整个事务** —— 包括同一 session 里
      刚执行的业务写入（建单）。那样"审计失败"会静默毁掉业务结果，
      是最坏的一类副作用。savepoint 只回滚审计这一条，业务不受影响。

    ⚠ `commit=False` 是默认：让审计随业务事务一起提交，
      保证"操作成功但审计丢失"不会发生（原子性）。
    """
    try:
        row = AuditLog(
            username=(user.username if user else "") or "",
            display_name=(user.display_name if user else "") or "",
            role=(user.role if user else "") or "",
            action=action,
            target=(target or "")[:128],
            detail=(json.dumps(detail, ensure_ascii=False, default=str)
                    if detail is not None else None),
            source=source,
            success=1 if success else 0,
            message=(message or "")[:255],
            ip=(ip or "")[:64],
        )
        if db is None:
            # 无 session（理论上不该发生）—— 明确告警，不静默丢
            logger.warning("audit: 未提供 db session，记录被丢弃 action=%s", action)
            return
        with db.begin_nested():          # savepoint：失败只回滚这一条
            db.add(row)
        if commit:
            db.commit()
    except Exception as e:
        # 这里**不能** rollback 整个 session（会毁掉业务写入）
        logger.warning("audit: 写入失败 action=%s err=%s", action, e)
