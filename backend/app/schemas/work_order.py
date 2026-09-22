"""工单管理 Pydantic Schemas"""

from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── 创建工单 ───────────────────────────────────────────

class WorkOrderCreate(BaseModel):
    customer_id: str
    customer_name: str
    geography: str = ""
    risk_level: str = "MEDIUM"
    probability: float = 0.0
    balance: float = 0.0
    risk_factors: list[str] = []
    strategy: str = ""
    assignee: str = ""
    note: str = ""
    # 分级依据快照 —— 前端可选传入；缺省时由后端补齐（见 routers/work_orders.py）
    thresholds_snapshot: dict | None = None
    model_used: str | None = None
    # 价值层快照 —— 同上，与 risk_level 快照配套（见 models/work_order.py 说明）
    value_tier_snapshot: str | None = None
    expected_value_snapshot: float | None = None
    # 渠道 —— 缺省时由后端按价值层预填；人工改选时须给 override_reason
    channel: str | None = None
    override_reason: str | None = None

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, v: str) -> str:
        allowed = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        if v.upper() not in allowed:
            raise ValueError(f"risk_level must be one of {allowed}")
        return v.upper()

    @field_validator("value_tier_snapshot")
    @classmethod
    def validate_value_tier(cls, v: str | None) -> str | None:
        if v is None:
            return None
        allowed = {"HIGH", "LOW", "ZERO"}
        if v.upper() not in allowed:
            raise ValueError(f"value_tier_snapshot must be one of {allowed}")
        return v.upper()

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, v: str | None) -> str | None:
        if v is None:
            return None
        allowed = {"relationship", "outbound", "automated"}
        if v not in allowed:
            raise ValueError(f"channel must be one of {allowed}")
        return v


# ── 批量建单 ───────────────────────────────────────────
#
# ⚠ 为什么批量建单要有独立 schema，而不是让前端循环调 POST /work-orders：
#   1) **每条可以有不同负责人与理由**（经理逐个分配）。前端循环也能做到，
#      但失败信息要前端自己拼，容易出现"三条成功一条失败但提示含糊"。
#   2) **一次事务、一次审计** —— 循环调接口会在审计表里留 N 条记录，
#      而用户的心理模型是"我做了一次批量操作"。
#   3) 服务端统一做互斥检查（已有进行中工单的人提前标出），
#      而不是等 409 再回头解释。

class BatchOrderItem(BaseModel):
    """批量建单中**单条**的指派信息。

    ⚠ 这里刻意**只放"人写的部分"**（负责人 / 理由），不放客户画像字段。
      画像（姓名/余额/风险等级/价值层）一律由**服务端按 customer_id
      重新查库**得到 —— 否则前端可以传一个与库里不符的余额，
      而工单里的快照会被当成"建单时的真实依据"永久留存。
      这类"客户端声称的事实"是不可信的（与 created_by 同一原则）。
    """
    customer_id: str = Field(min_length=1, max_length=32)
    assignee: str = Field(default="", max_length=64,
                          description="负责人（工号或姓名）；留空则由服务端取当前登录人")
    note: str = Field(default="", max_length=2000,
                      description="建单理由；留空则由服务端生成建议理由")


class WorkOrderBatchCreate(BaseModel):
    items: list[BatchOrderItem] = Field(min_length=1, max_length=50)


# ── 更新工单 ───────────────────────────────────────────

class WorkOrderUpdate(BaseModel):
    status: str | None = None       # pending / in_progress / completed / lost
    result: str | None = None       # retained / lost
    assignee: str | None = None
    strategy: str | None = None
    note: str | None = None
    risk_level: str | None = None
    probability: float | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"pending", "in_progress", "completed", "lost"}
        if v not in allowed:
            raise ValueError(f"status must be one of {allowed}")
        return v

    @field_validator("result")
    @classmethod
    def validate_result(cls, v: str | None) -> str | None:
        if v is None:
            return v
        allowed = {"retained", "lost"}
        if v not in allowed:
            raise ValueError(f"result must be one of {allowed}")
        return v


# ── 工单响应 ───────────────────────────────────────────

class WorkOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: str
    customer_name: str
    geography: str | None = None
    risk_level: str | None = None
    probability: float | None = None
    balance: float | None = None
    risk_factors: list[str] = []
    strategy: str | None = None
    status: str | None = "pending"
    result: str | None = None
    assignee: str | None = None
    note: str | None = None
    thresholds_snapshot: dict | None = None
    model_used: str | None = None
    value_tier_snapshot: str | None = None
    expected_value_snapshot: float | None = None
    channel: str | None = None
    channel_overridden: int | None = None
    override_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None

    @field_validator("risk_factors", mode="before")
    @classmethod
    def parse_risk_factors(cls, v: object) -> list[str]:
        """DB 中存 JSON 字符串，响应时转为 list"""
        if v is None:
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            import json
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, list) else [v]
            except json.JSONDecodeError:
                return [v]
        return []

    @field_validator("thresholds_snapshot", mode="before")
    @classmethod
    def parse_thresholds(cls, v: object) -> dict | None:
        """DB 中存 JSON 字符串，响应时转为 dict"""
        if v is None or isinstance(v, dict):
            return v
        if isinstance(v, str):
            import json
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None
