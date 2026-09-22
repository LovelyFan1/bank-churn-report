"""用户相关接口 —— 目前只提供「可指派负责人列表」。

**为什么需要这个接口**

派单必须从**真实存在的账号**里选人。此前 `assignee` 是自由文本，
前端只能给一个输入框 —— 于是 `seed_work_orders.py` 里的硬编码名单
（王晓芸 / 张思远 / 陈立群）被当成真人写进了 39 条工单，
而当时系统里并没有这些账号：**派单派给了查无此人**，
既无法登录办理，也无法按人统计工作量。

有了这个接口，前端可以（也应该）把输入框换成下拉。
后端在 `work_orders` 的建单/改派处同样会校验取值有效 ——
前端下拉是**体验**，服务端校验才是**边界**（直接调接口绕不过去）。

**为什么不复用 /api/auth/me**

那个接口返回的是「当前登录人」，这里的语义是「当前登录人能派给谁」，
两者受众不同：专员调 /me 是为了拿自己的权限，但他无权派单，
也不需要这个列表。故单独一个端点，并按权限收窄返回内容。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import ROLE_MANAGER, ROLE_STAFF, ROLE_ADMIN, User
from app.services import auth_deps
from app.services import auth_service as auth

router = APIRouter(prefix="/api/users", tags=["Users"])

# 可被指派为工单负责人的角色。
#
# ⚠ 为什么把 manager 也算进来：真实场景里经理之间会互相代班
#   （"我这周休假，这两单转给王芳"），且主管自己跟单很常见。
#   为什么不含 viewer：只读分析岗不办工单，派给他等于把单挂死。
ASSIGNABLE_ROLES = (ROLE_ADMIN, ROLE_MANAGER, ROLE_STAFF)


@router.get("/assignable")
async def list_assignable(user: User = Depends(auth_deps.current_user),
                          db: Session = Depends(get_db)):
    """可指派为工单负责人的行员列表。

    ⚠ 返回 `username`（工号）作为取值，`display_name` 作为显示文本。
      工单里应当存**工号**：姓名会遇上同名/改名，且无法关联回账号 ——
      而"我的工单"必须靠这个关联。详见 models/work_order.py 的说明。

    ⚠ 只返回启用账号（is_active=1）：停用（离职）的人不该再被派单。
      但**历史工单**里若已有此人，仍会原样显示（前端做映射失败时的兜底），
      不因为账号停用就让历史记录变成空白 —— 审计要的是"当时是谁"。
    """
    rows = (
        db.query(User)
        .filter(User.is_active == 1, User.role.in_(ASSIGNABLE_ROLES))
        .order_by(User.role, User.username)
        .all()
    )

    # 当前用户是否具备派单权 —— 前端据此决定"负责人"是下拉还是只读文本。
    # 专员没有 order:write，看不到派单 UI，但仍可能需要知道"我"是谁。
    can_assign = auth.has_perm(user.role or "", auth.PERM_ORDER_WRITE)

    return {
        "items": [
            {
                "username": u.username,
                "display_name": u.display_name,
                "department": u.department,
                "role": u.role,
                "role_label": auth.ROLE_LABELS.get(u.role, u.role),
            }
            for u in rows
        ],
        "can_assign": can_assign,
        # 前端可用它把工单里的工号渲染成姓名；同时作为"当前登录人"的默认值来源
        "me": {"username": user.username, "display_name": user.display_name},
    }
