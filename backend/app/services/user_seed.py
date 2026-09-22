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
from app.models.user import (ROLE_ADMIN, ROLE_MANAGER, ROLE_STAFF, ROLE_VIEWER,
                             User)
from app.services import auth_service as auth

logger = logging.getLogger(__name__)

# (行员号, 姓名, 部门, 角色, 是否预绑定 TOTP)
#
# ── 为什么有 5 个 staff ────────────────────────────────────
#
# 银行工单是**两个人的事**：经理建单派单，专员执行回填。
# 本项目原先只有 admin/manager/viewer 三种角色，没有"执行岗" ——
# 于是 work_orders.assignee 只能是自由文本，谁都能往里写任意名字。
#
# ⚠ 前三个 staff 的姓名**刻意取自 seed_work_orders.py 的 ASSIGNEES 名单**
#   （王晓芸 / 张思远 / 陈立群）。那批历史工单里有 39 条指派给了这三个人，
#   而当时系统里**并不存在**这些账号 —— 派单派给了查无此人。
#   现在把他们建成真实账号，历史数据就自洽了：主管能看到"王晓芸手上有
#   21 单"，王晓芸登录后也能在自己的工单列表里看到这些单。
#
#   这不是编造数据，而是**补上原本就该存在的账号**。
DEMO_USERS = [
    ("zhaomin",     "赵敏",  "信息科技部", ROLE_ADMIN,   True),
    ("liming",      "李铭",  "零售银行部", ROLE_MANAGER, False),
    ("wangfang",    "王芳",  "零售银行部", ROLE_MANAGER, False),
    ("chenjie",     "陈杰",  "风险管理部", ROLE_VIEWER,  False),
    # ── 客户专员（执行岗，只处理指派给自己的工单）──────────
    ("wangxiaoyun", "王晓芸", "零售银行部", ROLE_STAFF,   False),
    ("zhangsiyuan", "张思远", "零售银行部", ROLE_STAFF,   False),
    ("chenliqun",   "陈立群", "零售银行部", ROLE_STAFF,   False),
    ("liuyang",     "刘洋",  "零售银行部", ROLE_STAFF,   False),
    ("zhoujing",    "周静",  "零售银行部", ROLE_STAFF,   False),
]


def seed_users(db: Session, *, force: bool = False) -> dict:
    """播种演示账号（幂等）。

    ⚠ 为什么是**增量补齐**而不是"表非空就整体跳过"：
      原实现是 `if existing and not force: return 跳过`。加入 5 个 staff 后，
      已有环境（用户表里已有 4 人）会**永远拿不到这 5 个账号** ——
      表现为"代码里明明写了账号，登录却说用户名或口令不正确"，
      且没有任何报错。故改为**逐个账号检查**：缺谁补谁，已有的不动。

    ⚠ 已有账号的字段**不覆盖**：老用户可能已改过口令、改过姓名。
      用 force=True 才重置（供测试/重建演示环境用）。

    返回 {"seeded": bool, "created": n, "existing": m, "totp": {user: secret}}
    """
    existing = {u.username: u for u in db.query(User).all()}
    before = len(existing)

    created = 0
    totp: dict[str, str] = {}
    for username, name, dept, role, bind_totp in DEMO_USERS:
        if username in existing:
            # 老账号：不覆盖任何字段（可能已改口令/改名），仅记录其 TOTP
            if not force:
                continue
            u = existing[username]
        else:
            u = User(username=username)
            db.add(u)

        secret = auth.gen_totp_secret() if bind_totp else None
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
            created += 1

    db.commit()
    return {"seeded": created > 0, "created": created, "existing": before,
            "total": before + created, "totp": totp}


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
