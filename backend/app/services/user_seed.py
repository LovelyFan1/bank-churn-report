"""演示账号播种 —— 让系统开箱即可登录（幂等）。

**为什么单独一个文件、且在 preseed 阶段跑**

与数据播种同理：多 worker 下每个 worker 都会跑 startup，
check-then-act 会并发插入同一批用户（`username` 唯一约束会挡，
但失败的 worker 会抛异常）。故放在**单进程的 preseed 阶段**。

**为什么账号是硬编码的 4 个**

真银行的账号来自 HR 系统（AD 同步），本项目没有 HR。
故播种固定演示账号 —— 但**口令不硬编码**，从
`settings.AUTH_DEMO_PASSWORD` 取，便于演示时统一更换。

**双因素绑定策略**

`zhaomin`（管理员）**预绑定** TOTP，用于演示双因素流程；
其余账号**不预绑定**（`totp_enabled=0`），这样即使
`AUTH_REQUIRE_TOTP=true`，未绑定者仍可单因素登录 ——
否则演示时每个人都得先绑 App，太麻烦。

⚠ 这是**刻意的演示便利**，不是安全缺陷：代码路径本身支持强制双因素
  （见 auth.login 的判定），只是演示账号没绑。答辩被问到要如实说明。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.models.user import (ROLE_ADMIN, ROLE_MANAGER, ROLE_VIEWER, User)
from app.services import auth_service as auth

logger = logging.getLogger(__name__)

# (行员号, 姓名, 部门, 角色, 是否预绑定 TOTP)
DEMO_USERS = [
    ("zhaomin",  "赵敏",  "信息科技部",   ROLE_ADMIN,   True),
    ("liming",   "李铭",  "零售银行部",   ROLE_MANAGER, False),
    ("wangfang", "王芳",  "零售银行部",   ROLE_MANAGER, False),
    ("chenjie",  "陈杰",  "风险管理部",   ROLE_VIEWER,  False),
]


def seed_users(db: Session, *, force: bool = False) -> dict:
    """播种演示账号（幂等）。

    返回 {"seeded": bool, "created": n, "existing": m, "totp": {user: secret}}
    """
    existing = {u.username: u for u in db.query(User).all()}

    if existing and not force:
        return {"seeded": False, "created": 0, "existing": len(existing),
                "reason": "用户表已有数据，跳过"}

    created = 0
    totp: dict[str, str] = {}
    for username, name, dept, role, bind_totp in DEMO_USERS:
        if username in existing and not force:
            continue
        secret = auth.gen_totp_secret() if bind_totp else None
        u = existing.get(username) or User(username=username)
        u.display_name = name
        u.department = dept
        u.role = role
        # 演示口令统一从配置取；生产应由 HR 系统下发一次性口令
        u.password_hash = auth.hash_password(settings.AUTH_DEMO_PASSWORD)
        u.is_active = 1
        u.must_change_password = 0
        u.failed_attempts = 0
        u.locked_until = None
        if secret:
            u.totp_secret = secret
            u.totp_enabled = 1
            totp[username] = secret
        if username not in existing:
            db.add(u)
            created += 1

    db.commit()
    return {"seeded": True, "created": created, "existing": len(existing),
            "totp": totp}


def main() -> int:
    """命令行走口：python -m app.services.user_seed"""
    from app.database import Base, SessionLocal, engine
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        info = seed_users(db)
        print(f"[user_seed] {info}", flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    import sys
    sys.exit(main())
