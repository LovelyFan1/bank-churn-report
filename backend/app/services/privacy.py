"""客户信息脱敏 —— 让「只读分析」看得到分布，看不到具体是谁。

**为什么需要这一层**

银行业务里「看得到统计」与「看得到人」是**两种授权**：

    风控 / 审计 / 运营  需要分布与趋势来核对风险敞口、复核口径
    客户经理            需要具体名单来打电话

把两者混成一个 `data:view`，等于让所有能登录的人都能把 96,418 条
客户名单带走 —— 这既是权限过宽，也是个人信息保护上的问题。

故拆出 `customer:identify` 权限点：缺它时后端**脱敏**。

**为什么是"不返回"而不是"前端藏起来"**

只在前端隐藏列，数据仍在响应体里 —— 打开浏览器 devtools 或直接
curl 就能拿到全部姓名与余额，那不叫脱敏，叫**演给用户看**。
故本模块在**服务端**把字段真正去掉，前端连字段都收不到。

**为什么用白名单而不是黑名单**

黑名单（"把 surname/balance 删掉"）的失效方式是**静默**的：
将来 risk_scoring 新增一个 `phone` 字段，它会自动出现在脱敏响应里，
没有任何报错、没有任何测试会失败。

白名单（"只保留 risk_level / value_tier"）的失效方式是**显式**的：
新字段默认**不外泄**，只可能"少显示了"（立刻被发现并补进白名单），
不会"多泄露了"。失败方向决定了这个选择。

**保留什么、去掉什么（与用户确认过的口径）**

    保留   序号（客户 #N）、风险等级、价值层
    去掉   姓名、客户编号、余额、概率、期望价值、风险因素、
           建议动作/理由、渠道、年龄、性别、信用分、薪资 等

即"能看到这一批人有多危险、值多少钱"，但"不知道具体是谁、
为什么危险、该怎么联系"。
"""

from __future__ import annotations

from typing import Any

from app.models.user import User
from app.services import auth_service as auth

# 缺权限时给前端的说明 —— 必须写清"为什么看不到"，而不是让用户以为页面坏了
MASK_NOTICE = (
    "当前角色（只读分析）仅可查看聚合统计与匿名列表；"
    "客户姓名、编号、余额等身份信息需「客户经理」及以上权限。"
)

# 脱敏后**保留**的字段（白名单）—— 新增字段默认不外泄
_KEEP = ("risk_level", "value_tier", "has_active_order")


def can_identify(user: User | None) -> bool:
    """该用户是否可以看客户身份信息。

    ⚠ 默认**返回 False**（不可见）。若用户为 None 或角色未知，
      宁可少给权限也不多给 —— 与"默认拒绝"的鉴权中间件同一原则。
    """
    if user is None:
        return False
    return auth.has_perm(getattr(user, "role", "") or "",
                         auth.PERM_CUSTOMER_IDENTIFY)


# ── 工作台（全行大盘）专用判据 ────────────────────────────
#
# ⚠ 为什么工作台不能直接用 can_identify：
#
#   这两类角色的能力**正好相反**，只用 identify 会把 staff 放进来：
#
#     viewer  看得到全行分布，看不到客户是谁   identify=False → 会脱敏 ✅
#     staff   看得到客户是谁（要打电话），    identify=True  → **不脱敏** ❌
#             但不需要全行名单与大盘
#
#   staff 若在工作台看到"全行 Top10 客户姓名 + 余额"，等于绕过了
#   "他只看得到派给自己的单"这条数据级限制 —— 名单页与策略页都对他关了，
#   大盘却把同样的信息又递了出来，限制就白做了。
#
#   故工作台要求**两者同时具备**：既知道客户是谁（identify），
#   又有全行洞察授权（insight:view）。
#
#   结果：admin / manager 正常；viewer 脱敏（岗位需要）；staff 也脱敏
#   （他不是来看大盘的）。

def can_view_whole_book(user: User | None) -> bool:
    """能否在工作台看到**全行**的具名客户明细。"""
    if user is None:
        return False
    role = getattr(user, "role", "") or ""
    return (auth.has_perm(role, auth.PERM_CUSTOMER_IDENTIFY)
            and auth.has_perm(role, auth.PERM_INSIGHT_VIEW))


# 工作台脱敏时给前端的说明 —— 必须写清"为什么看不到"。
# ⚠ 与 MASK_NOTICE 分开：staff 被脱敏的原因不是"权限不够"（他有
#   identify 权限），而是"这个视图不是他的岗位职责"。用同一句话会
#   让他困惑"我明明能看客户姓名，为什么这里不行"。
MASK_NOTICE_SCOPED = (
    "当前角色（客户专员）仅处理指派给自己的工单；"
    "全行客户名单与策略分析需「客户经理」及以上权限。"
)


def mask_notice_for(user: User | None) -> str:
    """按角色挑合适的脱敏说明文案。

    staff 与 viewer 都会看到脱敏的工作台，但**原因不同**，
    文案必须区分，否则会给出误导性的解释。
    """
    role = getattr(user, "role", "") or ""
    if role == "staff":
        return MASK_NOTICE_SCOPED
    return MASK_NOTICE


def mask_customer(d: dict, seq: int | None = None) -> dict:
    """把一个客户记录脱敏成"匿名条目"。

    ⚠ `seq` 是**行内序号**，不是由 customer_id 派生的稳定假名。
      刻意如此：稳定假名（如 id 的哈希）能让攻击者跨页面把同一人串起来，
      从而逐步还原出个体画像（链接攻击）。序号只反映"在本次筛选结果中
      排第几"，不携带身份信息。
    """
    out: dict[str, Any] = {}
    for k in _KEEP:
        if k in d:
            out[k] = d[k]
    if seq is not None:
        out["seq"] = seq
        # 用固定文案占位，方便前端直接渲染而无需分支判断
        out["display_name"] = f"客户 #{seq}"
    return out


def mask_customers(items: list[dict], offset: int = 0) -> list[dict]:
    """批量脱敏。`offset` 是分页起点，保证跨页序号连续。"""
    return [mask_customer(d, seq=offset + i + 1)
            for i, d in enumerate(items or [])]


# 工单里**保留**的字段：工单自身的管理属性，不含客户身份
_ORDER_KEEP = (
    "id", "status", "result", "assignee", "risk_level",
    "value_tier_snapshot", "channel", "created_at", "updated_at",
    "completed_at", "model_used", "thresholds_snapshot",
)


def mask_order(d: dict, seq: int | None = None) -> dict:
    """把工单记录脱敏 —— 保留"这张单是什么状态、谁负责"，去掉"是谁的单"。

    ⚠ 刻意去掉 `note`：备注里常写着客户姓名与具体情况（系统生成的建单
      理由更是直接复述风险因素与余额）。留着它等于把脱敏白做了。
    """
    out: dict[str, Any] = {}
    for k in _ORDER_KEEP:
        if k in d:
            out[k] = d[k]
    if seq is not None:
        out["seq"] = seq
        out["display_name"] = f"客户 #{seq}"
    return out


def mask_orders(items: list[dict], offset: int = 0) -> list[dict]:
    return [mask_order(d, seq=offset + i + 1)
            for i, d in enumerate(items or [])]
