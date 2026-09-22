"""认证与授权服务 —— 4A 的认证( Authentication )与授权( Authorization )。

**设计原则：零新增依赖**

PBKDF2、HMAC、TOTP、JWT 全部用 Python 标准库实现（`hashlib` / `hmac` /
`base64` / `json`）。理由不是"省事"，而是这个项目的依赖面已经被严格
review 过（见 requirements.txt 里 LangGraph 那段的说明）—— 认证是安全
敏感模块，引入第三方库意味着把「口令怎么派生、令牌怎么签」交给别人，
而这几件事用标准库写清楚并不难，且**可审计**。

**与真银行的差距（必须如实说明，不能假装）**

    真银行                          本项目（仿真级）
    ─────────────────────────────   ─────────────────────────────
    AD/LDAP 为身份源，HR 驱动生命周期  本地 users 表 + 播种脚本
    UKey / 数字证书（国密 SM2）        TOTP 动态口令（同为密码技术）
    CAS / OIDC 单点登录               自签 JWT
    集中会话管理、单点登出             无状态 JWT + 前端超时

这是**刻意简化**：真 4A 是几十人年工程（含 CA 采购、目录服务、UKey 发放），
比赛环境无法落地。结构对齐、合规点可演示，是本项目的目标。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import time
from datetime import datetime, timedelta
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 口令派生（PBKDF2-HMAC-SHA256）
# ══════════════════════════════════════════════════════════

_PBKDF2_ITER = 120_000     # OWASP 对 PBKDF2-SHA256 的建议量级（≥600k 用于新系统，
                           # 此处取 12 万兼顾容器内登录延迟实测约 60ms）


def hash_password(password: str, *, iterations: int = _PBKDF2_ITER) -> str:
    """派生口令哈希。

    ⚠ 为什么每个口令用**独立随机盐**：否则相同口令得到相同哈希，
      攻击者用彩虹表可批量还原，且能从库里看出"这两个人密码一样"。

    ⚠ 为什么迭代次数写进结果串：将来提高迭代数时，老哈希仍可验证
      （用其自带的参数），无需强制所有人改密。这是可演进的做法。
    """
    if not password:
        raise ValueError("口令不能为空")
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验口令 —— 用**常量时间比较**，防时序侧信道。"""
    if not password or not stored:
        return False
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, int(iters))
        return hmac.compare_digest(dk, expect)
    except Exception:
        # 存储格式损坏 —— 当作校验失败，绝不放行
        logger.warning("auth: 口令哈希格式异常")
        return False


# ══════════════════════════════════════════════════════════
# TOTP（RFC 6238）—— 双因素的第二个因子
# ══════════════════════════════════════════════════════════

def gen_totp_secret() -> str:
    """生成 base32 密钥（无填充，适配主流 Authenticator App）。"""
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32 + pad)
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def totp_now(secret_b32: str, step: int = 30, at: float | None = None) -> str:
    """当前动态口令（30 秒一步）。"""
    t = int(at if at is not None else time.time())
    return _hotp(secret_b32, t // step)


def totp_verify(secret_b32: str, code: str, *, window: int = 1,
                step: int = 30, at: float | None = None) -> bool:
    """校验动态口令，允许前后各一个时间窗。

    ⚠ 为什么允许漂移：客户端与服务器的时钟很难完全一致，不允许漂移会
      导致用户"输对了却登不上"。窗口取 ±1 步（共 90 秒）是业界通行折中；
      放太宽（如 ±5）会显著增大暴力猜测空间。
    """
    if not secret_b32 or not code:
        return False
    code = str(code).strip().replace(" ", "")
    if not code.isdigit():
        return False
    t = int(at if at is not None else time.time())
    cur = t // step
    for w in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret_b32, cur + w), code):
            return True
    return False


def totp_uri(secret_b32: str, username: str) -> str:
    """otpauth:// URI —— 供前端渲染二维码，用 App 扫码绑定。"""
    from urllib.parse import quote
    label = quote(f"{settings.APP_NAME}:{username}")
    return (f"otpauth://totp/{label}?secret={secret_b32}"
            f"&issuer={quote(settings.APP_NAME)}&algorithm=SHA1"
            f"&digits=6&period=30")


# ══════════════════════════════════════════════════════════
# JWT（HS256）—— 会话票据
# ══════════════════════════════════════════════════════════
#
# ⚠ 为什么用 JWT 而不是服务端 session：
#   生产是 `uvicorn --workers 4`。服务端 session 存进程内存会有和
#   Agent 上下文同样的问题（4 个 worker 各自为政）；存 Redis 则引入
#   新的有状态依赖。JWT 自包含且无状态，多 worker 天然一致。
#
# ⚠ JWT 的**已知代价**（不隐瞒）：签发后无法单方吊销，只能等过期。
#   真银行用短有效期 + 集中会话管理解决。本项目有效期取得较短
#   （见 settings.AUTH_TOKEN_TTL_MINUTES），并在审计里留痕，
#   但**没有**实现吊销列表 —— 这是简化，答辩时应如实说明。

def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * ((4 - len(s) % 4) % 4))


def _secret_file() -> Path:
    """签名密钥的落盘位置 —— 与 SQLite 同目录（都是 `backend_data` 卷）。

    ⚠ 为什么必须落盘而不能"每个进程各生成一个随机密钥"：
      生产是 `uvicorn --workers 4`（外加 celery worker），**每个进程独立
      初始化**。若各自随机，A 签发的令牌到 B 那里验签必然失败 ——
      表现为"登录成功但一刷新就掉线"，且**随机出现**（取决于命中哪个
      worker），是最难排查的一类故障。落盘共享才能既随机又一致。
    """
    from app.config import settings as _s
    # DATABASE_URL 形如 sqlite:///./data/churn_analysis.db → 取其所在目录
    url = _s.DATABASE_URL or ""
    if url.startswith("sqlite:///"):
        db_path = Path(url.replace("sqlite:///", "", 1))
        return db_path.parent / ".auth_secret"
    return Path("data/.auth_secret")


def _load_or_create_secret() -> str:
    """取签名密钥：优先配置 → 其次读文件 → 都没有则生成并原子落盘。

    ⚠ 这里替换掉了早先的"源码内默认密钥"实现。那是**真实可利用的漏洞**，
      实测（t_security_probe.py）确认：默认密钥写在源码里即等同于公开，
      任何人都能伪造管理员令牌并读取全部 96,418 条客户数据。

      修法不是"再换个更隐蔽的常量"（同样会被读到），而是**让源码里
      根本不存在可用密钥** —— 未配置时随机生成。

    ⚠ 用 `O_CREAT | O_EXCL` 而非"先判断再写"：
      多 worker 会同时首次启动，check-then-act 存在竞态 ——
      两个进程都写，后者覆盖前者，先启动的 worker 立刻失去验签能力。
      O_EXCL 保证只有一个创建成功，其余读取赢家的密钥。
    """
    configured = (settings.AUTH_SECRET_KEY or "").strip()
    if configured:
        return configured

    path = _secret_file()
    try:
        if path.exists():
            val = path.read_text(encoding="utf-8").strip()
            if val:
                return val
    except Exception as e:
        logger.warning("auth: 读取密钥文件失败 %s: %s", path, e)

    generated = secrets.token_urlsafe(48)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, generated.encode("utf-8"))
        finally:
            os.close(fd)
        logger.warning(
            "auth: AUTH_SECRET_KEY 未配置，已生成随机密钥并写入 %s（权限 600）。"
            "生产建议在 .env 中显式设置，以便密钥轮换与多环境统一。", path)
        return generated
    except FileExistsError:
        # 另一个 worker 赢了竞态 —— 读它写的
        try:
            val = path.read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception as e:
            logger.error("auth: 密钥文件存在但读取失败 %s: %s", path, e)
        raise RuntimeError(
            "无法取得签名密钥：文件已存在但不可读。请检查数据卷权限，"
            "或显式设置 AUTH_SECRET_KEY。")
    except Exception as e:
        # 落盘失败（只读文件系统等）—— 明确失败，不静默退回固定值
        raise RuntimeError(
            f"无法创建签名密钥文件 {path}：{type(e).__name__}: {e}。"
            f"请显式设置 AUTH_SECRET_KEY 环境变量。") from e


def _jwt_secret() -> bytes:
    """签名密钥（进程内缓存）。

    ⚠ 缓存是安全的：它来自配置或共享文件，所有 worker 看到同一份。
      缓存只为避免每请求读一次磁盘。
    """
    global _SECRET_CACHE
    if _SECRET_CACHE is None:
        _SECRET_CACHE = _load_or_create_secret()
    return _SECRET_CACHE.encode("utf-8")


_SECRET_CACHE: str | None = None


def make_token(payload: dict, ttl_minutes: int | None = None) -> str:
    """签发 JWT。payload 里至少应有 sub（用户名）与 role。"""
    ttl = ttl_minutes if ttl_minutes is not None else settings.AUTH_TOKEN_TTL_MINUTES
    now = int(time.time())
    body = dict(payload)
    body.update({
        "iat": now,
        "exp": now + ttl * 60,
        "jti": secrets.token_hex(8),
    })
    header = {"alg": "HS256", "typ": "JWT"}
    seg = (_b64u(json.dumps(header, separators=(",", ":")).encode()) + "." +
           _b64u(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()))
    sig = hmac.new(_jwt_secret(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64u(sig)


class TokenError(Exception):
    """令牌无效（格式/签名/过期）—— 统一异常，调用方据此返回 401。"""


def decode_token(token: str) -> dict:
    """验签并解析 JWT。任何问题都抛 TokenError，绝不返回"部分可信"的结果。

    ⚠ 顺序很重要：**先验签，再读 payload**。若先解析再校验，
      一个精心构造的 payload 可能在验签前就被用到（经典 JWT 漏洞）。
    """
    if not token or token.count(".") != 2:
        raise TokenError("令牌格式不正确")
    seg, sig_b64 = token.rsplit(".", 1)
    expect = hmac.new(_jwt_secret(), seg.encode(), hashlib.sha256).digest()
    try:
        got = _b64u_dec(sig_b64)
    except Exception:
        raise TokenError("令牌签名无法解码")
    if not hmac.compare_digest(expect, got):
        raise TokenError("令牌签名校验失败")
    try:
        payload = json.loads(_b64u_dec(seg.split(".", 1)[1]))
    except Exception:
        raise TokenError("令牌内容无法解析")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise TokenError("令牌已过期，请重新登录")
    return payload


# ══════════════════════════════════════════════════════════
# 授权（RBAC）
# ══════════════════════════════════════════════════════════
#
# 权限点按「资源:动作」命名。角色 → 权限集合的映射就是授权策略的**唯一**
# 来源，前端也调 /api/auth/me 拿到同一份，避免前后端各写一套权限表
# （那必然会出现"按钮能点但接口拒绝"的不一致）。

PERM_VIEW = "data:view"            # 查看客户/工单/分析
PERM_ORDER_WRITE = "order:write"   # 建单/改单/删单
PERM_AGENT_USE = "agent:use"       # 使用智能助手
PERM_USER_ADMIN = "user:admin"     # 用户管理
PERM_AUDIT_VIEW = "audit:view"     # 查看审计日志
# 重算力/全局性操作：模型重训、聚类标签落库。
# ⚠ 为什么单独一个权限点而不是并入 order:write：
#   这两类操作会**改变全系统的判定基准** —— 模型重训会移动风险分级线与
#   决策阈值，聚类落库会覆盖全部客户的分群标签。它们的影响面比"建一张工单"
#   大几个数量级，不该让客户经理误触。故只给管理员。
PERM_MODEL_TRAIN = "model:train"

PERMISSIONS = {
    "admin": [PERM_VIEW, PERM_ORDER_WRITE, PERM_AGENT_USE,
              PERM_USER_ADMIN, PERM_AUDIT_VIEW, PERM_MODEL_TRAIN],
    "manager": [PERM_VIEW, PERM_ORDER_WRITE, PERM_AGENT_USE],
    "viewer": [PERM_VIEW],
}

ROLE_LABELS = {
    "admin": "系统管理员",
    "manager": "客户经理",
    "viewer": "只读分析",
}


def permissions_of(role: str) -> list[str]:
    return list(PERMISSIONS.get(role or "", []))


def has_perm(role: str, perm: str) -> bool:
    return perm in PERMISSIONS.get(role or "", [])


# ══════════════════════════════════════════════════════════
# 登录失败处理（等保三级要求）
# ══════════════════════════════════════════════════════════

def lock_seconds(attempts: int) -> int:
    """按失败次数决定锁定时长（递增，防在线暴力破解）。

    实测口径：5 次以内不锁（容忍手误），第 5 次起锁定。
    递增而非固定，是为了让自动化爆破的代价随时间线性上升。
    """
    if attempts < settings.AUTH_MAX_FAILED:
        return 0
    over = attempts - settings.AUTH_MAX_FAILED
    return min(60 * (2 ** over), 3600)      # 1min → 2 → 4 … 上限 1 小时


def now() -> datetime:
    return datetime.now()


# ══════════════════════════════════════════════════════════
# 服务间内部令牌
# ══════════════════════════════════════════════════════════

def internal_token() -> str:
    """内部调用令牌 —— Agent 工具层回调自身接口时携带。

    ⚠ 它是**服务级**身份，不是某个用户。故工具层的调用不代表任何行员；
      真正代表行员的写操作发生在 `/api/agent/confirm`（由用户令牌触发，
      操作者从会话取）。这个分工是刻意的：
      工具层只查询，写操作永远经由用户显式确认。
    """
    if settings.AUTH_INTERNAL_TOKEN:
        return settings.AUTH_INTERNAL_TOKEN
    # 由签名密钥派生：HMAC 保证即使签名密钥泄露也无法从中"看出"它，
    # 且不引入第二个需要管理的秘密。
    return hmac.new(_jwt_secret(), b"internal-tool-call",
                    hashlib.sha256).hexdigest()


def is_internal_request(token: str, client_host: str) -> bool:
    """判断是否为合法的内部调用 —— **令牌与来源必须同时满足**。

    ⚠ 只校验令牌不校验来源，则令牌一旦泄露（日志、报错信息）即可被
      外部重放；只校验来源则容器内任意进程都能绕过鉴权。
      两者叠加把攻击面收缩到"已能在容器内执行代码"。
    """
    if not token:
        return False
    expect = internal_token()
    if not hmac.compare_digest(token, expect):
        return False
    host = (client_host or "").strip()
    # 回环地址白名单 —— 容器内工具层走 127.0.0.1
    return host in ("127.0.0.1", "::1", "localhost", "testclient")
