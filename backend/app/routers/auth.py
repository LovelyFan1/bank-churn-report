"""认证 Router —— /api/auth/*

流程（对齐 4A 的「认证」环节）：

    POST /login      第一步：行员号 + 口令
                     → 需要双因素时返回 need_totp=true + 临时票据（不返回正式令牌）
    POST /login/totp 第二步：临时票据 + 动态口令
                     → 返回正式令牌
    GET  /me         当前身份 + 权限清单（前端据此控制按钮可见性）
    POST /logout     登出（写审计；JWT 无状态故服务端不销毁令牌）
    POST /change-password  改口令

**为什么登录分两步、中间用"临时票据"而不是两次都传口令**

若第二步还要重传口令，等于口令在网络上多传一次、前端多存一份 ——
徒增泄露面。临时票据是本服务签发的短期 JWT（`scope=pre_totp`，
有效期 5 分钟），只能用于换正式令牌，**不能**访问任何业务接口
（`current_user` 会拒绝非 full scope 的令牌）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.services import auth_deps as deps
from app.services import auth_service as auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# 临时票据有效期（分钟）—— 够输一次动态口令即可，不宜长
_PRE_TOTP_TTL = 5

# ⚠ 统一的登录失败措辞：**不区分**"用户不存在"与"口令错误"。
#   区分会泄露"哪些行员号是有效的"，给攻击者提供用户名枚举。
#   （审计日志里记录真实原因，供管理员排查 —— 用户看不到。）
_BAD_CRED = "行员号或口令不正确"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TotpRequest(BaseModel):
    ticket: str = Field(min_length=8)
    code: str = Field(min_length=4, max_length=10)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class DemoTotpRequest(BaseModel):
    """演示取码的入参 —— **只收票据**。

    ⚠ 为什么不复用 TotpRequest：那个模型带 `code`（min_length=4，因为它是
      校input）。展示接口不需要 code，复用会导致前端必须传一个占位值，
      而占位值长度不足时被 422 拦下 —— 实测踩过：前端传 '0' 结果整个
      演示区渲染为空，且报错出现在网络层（页面看不到原因）。
      **按用途定义入参，不要为了省一个类而复用。**
    """
    ticket: str = Field(min_length=8)


def _user_public(u: User) -> dict:
    """对外暴露的用户信息 —— 绝不含 password_hash / totp_secret。"""
    return {
        "username": u.username,
        "display_name": u.display_name or u.username,
        "department": u.department or "",
        "role": u.role,
        "role_label": auth.ROLE_LABELS.get(u.role, u.role),
        "permissions": auth.permissions_of(u.role),
        "totp_enabled": bool(u.totp_enabled),
        "must_change_password": bool(u.must_change_password),
        "last_login_at": (u.last_login_at.isoformat()
                          if u.last_login_at else None),
    }


def _issue(u: User, request: Request, db: Session,
           message: str = "登录成功") -> dict:
    """签发正式令牌并写登录审计。

    ⚠ 必须传 db —— 早先版本传 db=None，导致 write_audit 内部
      `db.add()` 抛 AttributeError 被 except 吞掉，**审计静默丢失**
      却不报错。登录审计是 4A 的硬要求，不能在"看起来成功了"的情况下丢。
    """
    token = auth.make_token({"sub": u.username, "role": u.role,
                             "scope": "full", "name": u.display_name or ""})
    u.last_login_at = auth.now()
    u.last_login_ip = deps._client_ip(request)
    u.failed_attempts = 0
    u.locked_until = None
    deps.write_audit(db, user=u, action="login", source="ui",
                     success=True, message=message,
                     ip=deps._client_ip(request))
    return {"token": token, "user": _user_public(u),
            "expires_in": settings.AUTH_TOKEN_TTL_MINUTES * 60,
            "idle_timeout": settings.AUTH_IDLE_MINUTES * 60}


@router.get("/me")
async def me(user: User = Depends(deps.current_user)):
    """当前登录人 + 权限清单。

    ⚠ 前端**必须**用这个接口驱动按钮可见性，而不是自己写一份角色判断 ——
      两处各写一套必然出现"按钮能点但接口 403"。
    """
    return {
        "user": _user_public(user),
        "auth_enabled": settings.AUTH_ENABLED,
        "idle_timeout": settings.AUTH_IDLE_MINUTES * 60,
        "token_ttl": settings.AUTH_TOKEN_TTL_MINUTES * 60,
    }


@router.get("/policy")
async def policy():
    """登录策略（免鉴权）—— 登录页据此决定是否显示动态口令输入框。

    ⚠ 只暴露**策略**，不暴露任何用户信息。
    """
    return {
        "auth_enabled": settings.AUTH_ENABLED,
        "require_totp": settings.AUTH_REQUIRE_TOTP,
        "max_failed": settings.AUTH_MAX_FAILED,
        "idle_minutes": settings.AUTH_IDLE_MINUTES,
        "password_policy": {
            "min_length": 8,
            "require_letter_and_digit": True,
        },
        # 演示辅助开关 —— 前端据此决定要不要显示"取动态口令"入口
        "demo_show_totp": bool(settings.AUTH_DEMO_SHOW_TOTP),
    }


@router.post("/demo/totp")
async def demo_totp(body: DemoTotpRequest, request: Request,
                    db: Session = Depends(get_db)):
    """**演示辅助**：用临时票据换当前动态口令。

    ⚠ 这是刻意提供的演示便利，不是安全设计 —— 见 config 里
      AUTH_DEMO_SHOW_TOTP 的说明。它把第二因子公开给了"能打开登录页
      且知道口令"的人，等同于取消第二因子的意义。

    为何仍然做了三重约束（而不是干脆不加）：
      1. **必须 AUTH_DEMO_SHOW_TOTP=true**，默认关；关时返回 404（不是 403）——
         未开启的功能就该像不存在，避免暴露"这里本来有个口子"
      2. **必须持有效临时票据**（即已通过第一因子口令），
         故陌生人在登录页拿不到任何人的口令
      3. 只对**已绑定 TOTP** 的用户返回，且返回的是**当前 30 秒窗口**的
         口令 —— 它本身会过期，不是长期凭据

    ⚠ 与 /login/totp 的区别：那个是**校验**，这个是**展示**。
      分离成两个端点，是为了让"展示"永远无法被用来直接换取令牌 ——
      拿到展示的口令后仍要走 /login/totp 正常流程。
    """
    if not settings.AUTH_DEMO_SHOW_TOTP:
        # ⚠ 返回 404 而非 403：未启用的功能不该对外暴露其存在
        raise HTTPException(status_code=404, detail="Not Found")

    try:
        payload = auth.decode_token(body.ticket)
    except auth.TokenError as e:
        raise HTTPException(status_code=401, detail=str(e))

    if payload.get("scope") != "pre_totp":
        raise HTTPException(status_code=401, detail="票据类型不正确")

    u = db.query(User).filter(User.username == payload.get("sub")).first()
    if u is None or not u.is_active:
        raise HTTPException(status_code=401, detail="用户不可用")
    if not (u.totp_enabled and u.totp_secret):
        raise HTTPException(status_code=404, detail="该账号未绑定动态口令")

    import time as _t
    now_ts = _t.time()
    return {
        "username": u.username,
        "display_name": u.display_name,
        "code": auth.totp_now(u.totp_secret),
        # 口令每 30 秒一变；给出剩余秒数供前端倒计时
        "expires_in": 30 - int(now_ts % 30),
        # 密钥与 otpauth URI 供扫码绑定（换机器不必重绑）
        "secret": u.totp_secret,
        "uri": auth.totp_uri(u.totp_secret, u.username),
        "warning": "演示辅助：此接口公开第二因子，仅限演示环境，生产必须关闭",
    }


@router.post("/login")
async def login(body: LoginRequest, request: Request,
                db: Session = Depends(get_db)):
    """第一步：行员号 + 口令。"""
    if not settings.AUTH_ENABLED:
        raise HTTPException(status_code=400, detail="鉴权已关闭，无需登录")

    username = body.username.strip()
    ip = deps._client_ip(request)
    u = db.query(User).filter(User.username == username).first()

    # ── 锁定检查（在任何凭据校验之前）─────────────────────
    # ⚠ 顺序很关键：先判锁定，再做昂贵的 PBKDF2 计算。
    #   否则攻击者在锁定期间仍能迫使服务器做 12 万次哈希 —— 变成放大器。
    if u is not None and u.locked_until and u.locked_until > auth.now():
        left = int((u.locked_until - auth.now()).total_seconds())
        deps.write_audit(db, user=None, action="login", target=username,
                         success=False, message=f"账号锁定中，剩余 {left}s",
                         ip=ip, commit=True)
        raise HTTPException(
            status_code=423,
            detail=f"账号已锁定，请 {max(1, left // 60)} 分钟后再试",
        )

    ok = u is not None and auth.verify_password(body.password, u.password_hash)
    if not ok:
        lock = 0
        # 失败计数——用户不存在时不计数（无对象可计），但仍给相同的措辞
        if u is not None:
            u.failed_attempts = (u.failed_attempts or 0) + 1
            lock = auth.lock_seconds(u.failed_attempts)
            if lock:
                u.locked_until = auth.now() + timedelta(seconds=lock)
            db.commit()
        deps.write_audit(db, user=None, action="login", target=username,
                         success=False,
                         message="用户不存在" if u is None else "口令错误",
                         ip=ip, commit=True)
        # ⚠ 触发锁定的**这一次**就回 423，而不是等到下一次。
        #   早先只在后续请求上判锁定，导致第 5 次仍返回"口令不正确"，
        #   用户不知道自己已被锁、会继续试 —— 体验与安全性都更差
        #   （实测：测试期望 423 却拿到 401 才发现）。
        if lock:
            raise HTTPException(
                status_code=423,
                detail=f"口令错误次数过多，账号已锁定 "
                       f"{max(1, lock // 60)} 分钟",
            )
        raise HTTPException(status_code=401, detail=_BAD_CRED)

    if not u.is_active:
        deps.write_audit(db, user=None, action="login", target=username,
                         success=False, message="账号已停用", ip=ip, commit=True)
        raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")

    # ── 双因素判定 ───────────────────────────────────────
    # 全局要求 + 该用户已绑定，两者都满足才走第二步。
    # 若全局要求但用户**没绑定**，则不强制（否则该用户永远登不上）；
    # 这一点在 /me 里如实反映 totp_enabled=false。
    if settings.AUTH_REQUIRE_TOTP and u.totp_enabled:
        ticket = auth.make_token(
            {"sub": u.username, "scope": "pre_totp", "role": u.role},
            ttl_minutes=_PRE_TOTP_TTL)
        return {"need_totp": True, "ticket": ticket,
                "expires_in": _PRE_TOTP_TTL * 60}

    # ⚠ 必须 commit，且**必须在 _issue 之后**：_issue 里的登录审计、
    #   last_login_at / failed_attempts 重置都只是挂在 session 上。
    #   早先这条路径漏了 commit，导致**单因素登录完全不留审计**
    #   （实测：审计表里只有失败记录），而登录审计是 4A 的硬要求。
    #   它不会报错，只是安静地什么都没有。
    out = _issue(u, request, db)
    db.commit()
    return {"need_totp": False, **out}


@router.post("/login/totp")
async def login_totp(body: TotpRequest, request: Request,
                     db: Session = Depends(get_db)):
    """第二步：临时票据 + 动态口令 → 正式令牌。"""
    ip = deps._client_ip(request)
    try:
        payload = auth.decode_token(body.ticket)
    except auth.TokenError as e:
        raise HTTPException(status_code=401, detail=str(e))

    if payload.get("scope") != "pre_totp":
        # ⚠ 关键防线：正式令牌不能在此时被"二次利用"换新令牌
        raise HTTPException(status_code=401, detail="票据类型不正确")

    u = db.query(User).filter(User.username == payload.get("sub")).first()
    if u is None or not u.is_active:
        raise HTTPException(status_code=401, detail="用户不可用")

    if not auth.totp_verify(u.totp_secret or "", body.code):
        u.failed_attempts = (u.failed_attempts or 0) + 1
        lock = auth.lock_seconds(u.failed_attempts)
        if lock:
            u.locked_until = auth.now() + timedelta(seconds=lock)
        db.commit()
        deps.write_audit(db, user=None, action="login_totp", target=u.username,
                         success=False, message="动态口令错误", ip=ip, commit=True)
        raise HTTPException(status_code=401, detail="动态口令不正确或已过期")

    out = _issue(u, request, db, message="登录成功（双因素）")
    db.commit()
    return out


@router.post("/logout")
async def logout(request: Request,
                 user: User = Depends(deps.current_user),
                 db: Session = Depends(get_db)):
    """登出。

    ⚠ 必须如实说明：JWT 是无状态的，服务端**无法**让已签发的令牌失效。
      此接口只写审计、前端丢弃令牌。真银行靠集中会话管理解决。
    """
    deps.write_audit(db, user=user, action="logout", source="ui",
                     message="用户登出", ip=deps._client_ip(request), commit=True)
    return {"ok": True,
            "note": "服务端已记录登出；令牌为无状态 JWT，请前端一并丢弃"}


@router.post("/change-password")
async def change_password(body: ChangePasswordRequest, request: Request,
                          user: User = Depends(deps.current_user),
                          db: Session = Depends(get_db)):
    """修改口令（含首次登录强制改密的路径）。

    ⚠ 这里**必须重新查库**取真实的 user 对象，不能用 `current_user` 返回的那个：
      中间件命中缓存时会构造一个**未绑定 session、且不含 password_hash**
      的轻量 User（见 auth_deps.current_user）。若直接拿它校验旧口令，
      `user.password_hash` 是 None → verify_password 恒为 False →
      **任何原口令都被判错，改密永远失败**（实测症状正是如此）。
    """
    u = db.query(User).filter(User.username == user.username).first()
    if u is None:
        raise HTTPException(status_code=401, detail="用户不存在，请重新登录")

    if not auth.verify_password(body.old_password, u.password_hash):
        deps.write_audit(db, user=u, action="change_password",
                         success=False, message="原口令错误",
                         ip=deps._client_ip(request), commit=True)
        raise HTTPException(status_code=401, detail="原口令不正确")

    err = _check_password_strength(body.new_password)
    if err:
        raise HTTPException(status_code=422, detail=err)

    if auth.verify_password(body.new_password, u.password_hash):
        raise HTTPException(status_code=422, detail="新口令不能与原口令相同")

    u.password_hash = auth.hash_password(body.new_password)
    u.must_change_password = 0
    deps.write_audit(db, user=u, action="change_password", success=True,
                     message="口令已修改", ip=deps._client_ip(request))
    db.commit()
    return {"ok": True}


def _check_password_strength(pw: str) -> str | None:
    """口令强度检查 —— 返回错误说明，None 表示通过。

    ⚠ 刻意**不**强制"必须含特殊字符"：那类规则会促使用户写
      `Password@1` 这种可预测口令，反而降低实际安全性
      （NIST SP 800-63B 已建议不强制组合规则、改为查弱口令表）。
      本项目检查长度 + 字母数字混合，属于折中。
    """
    if len(pw) < 8:
        return "新口令至少 8 位"
    has_alpha = any(c.isalpha() for c in pw)
    has_digit = any(c.isdigit() for c in pw)
    if not (has_alpha and has_digit):
        return "新口令需同时包含字母与数字"
    if pw.lower() in ("bank@2025", "password", "12345678", "admin123"):
        return "该口令过于常见，请更换"
    return None


@router.get("/audit")
async def audit_list(limit: int = 50, action: str | None = None,
                     username: str | None = None,
                     user: User = Depends(
                         deps.require_perm(auth.PERM_AUDIT_VIEW)),
                     db: Session = Depends(get_db)):
    """审计日志查询 —— 仅管理员可见（4A 的 Audit 环节）。

    ⚠ 审计日志本身也是敏感数据（含操作者与时间规律），故限权访问。
    """
    from app.models.user import AuditLog
    limit = max(1, min(int(limit or 50), 200))
    q = db.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    if username:
        q = q.filter(AuditLog.username == username)
    rows = q.order_by(AuditLog.id.desc()).limit(limit).all()
    return {
        "total": db.query(AuditLog).count(),
        "items": [{
            "id": r.id,
            "username": r.username,
            "display_name": r.display_name,
            "role": r.role,
            "role_label": auth.ROLE_LABELS.get(r.role, r.role),
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
            "source": r.source,
            "success": bool(r.success),
            "message": r.message,
            "ip": r.ip,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }
