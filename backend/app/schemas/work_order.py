"""工单管理 Pydantic Schemas"""

from datetime import datetime
from pydantic import BaseModel, ConfigDict, field_validator


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

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, v: str) -> str:
        allowed = {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
        if v.upper() not in allowed:
            raise ValueError(f"risk_level must be one of {allowed}")
        return v.upper()


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
