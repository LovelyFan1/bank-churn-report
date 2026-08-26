"""客户管理 Router — 全量客户列表 + 筛选/排序/分页 + 实时风险打分。"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.customer_service import list_customers

router = APIRouter(prefix="/api/customers", tags=["Customers"])


@router.get("")
async def get_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, description="客户编号 / 姓名模糊搜索"),
    geography: str | None = Query(default=None, description="France / Germany / Spain"),
    risk_level: str | None = Query(default=None, description="CRITICAL / HIGH / MEDIUM / LOW"),
    exited: int | None = Query(default=None, description="1=已流失, 0=未流失"),
    has_order: bool | None = Query(default=None, description="是否已有进行中工单"),
    sort_by: str = Query(default="probability", description="probability / balance / age / credit_score"),
    sort_order: str = Query(default="desc", description="asc / desc"),
    db: Session = Depends(get_db),
):
    """客户列表 — 支持搜索、筛选、排序、分页，返回实时风险打分。"""
    return list_customers(
        db=db,
        page=page,
        page_size=page_size,
        search=search,
        geography=geography,
        risk_level=risk_level,
        exited=exited,
        has_order=has_order,
        sort_by=sort_by,
        sort_order=sort_order,
    )
