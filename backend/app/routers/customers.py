"""客户管理 Router — 全量客户列表 + 筛选/排序/分页 + 单客户详情 + 导出。

路由顺序注意：`/export` 必须在 `/{customer_id}` **之前**注册，
否则 "export" 会被当成 customer_id 匹配掉。
"""

import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.customer_service import (
    _filtered_customers,
    get_customer_detail,
    list_customers,
)
from app.services import risk_scoring

router = APIRouter(prefix="/api/customers", tags=["Customers"])

# 导出列 —— 面向"拿去打电话"的场景，只保留执行时真正用得到的字段。
# 内部字段（id/exited）不导出，避免把训练标签泄露给一线。
_EXPORT_COLUMNS = [
    ("customer_id", "客户编号"),
    ("surname", "姓名"),
    ("geography", "地区"),
    ("age", "年龄"),
    ("balance", "余额"),
    ("risk_level", "风险等级"),
    ("probability", "流失概率"),
    ("value_tier", "价值层"),
    ("expected_value", "期望价值"),
    ("channel", "触达渠道"),
    ("action", "建议动作"),
    ("reason", "建议理由"),
    ("risk_factors", "风险因素"),
]

_TIER_CN = {"HIGH": "高价值", "LOW": "低价值", "ZERO": "零余额"}


@router.get("")
async def get_customers(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, description="客户编号 / 姓名模糊搜索"),
    geography: str | None = Query(default=None, description="France / Germany / Spain"),
    risk_level: str | None = Query(default=None, description="CRITICAL / HIGH / MEDIUM / LOW"),
    value_tier: str | None = Query(default=None, description="HIGH / LOW / ZERO"),
    exited: int | None = Query(default=None, description="1=已流失, 0=未流失"),
    has_order: bool | None = Query(default=None, description="是否已有进行中工单"),
    sort_by: str = Query(default="probability", description="probability / expected_value / balance / age / credit_score"),
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
        value_tier=value_tier,
        exited=exited,
        has_order=has_order,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.get("/export")
async def export_customers(
    search: str | None = Query(default=None),
    geography: str | None = Query(default=None),
    risk_level: str | None = Query(default=None),
    value_tier: str | None = Query(default=None),
    exited: int | None = Query(default=None),
    has_order: bool | None = Query(default=None),
    sort_by: str = Query(default="expected_value"),
    sort_order: str = Query(default="desc"),
    db: Session = Depends(get_db),
):
    """导出当前筛选结果为 CSV。

    与列表页共用 `_filtered_customers`，保证「导出的」与「屏幕上看到的」
    是同一批人 —— 否则一线拿着名单打电话，会发现和系统里对不上。

    编码用 utf-8-sig（带 BOM）：Excel 打开中文 CSV 不带 BOM 会乱码。
    """
    items, _ = _filtered_customers(
        db, search=search, geography=geography, risk_level=risk_level,
        value_tier=value_tier, exited=exited, has_order=has_order,
    )

    key_map = {
        "probability": "probability", "expected_value": "expected_value",
        "balance": "balance", "age": "age", "credit_score": "credit_score",
    }
    key = key_map.get(sort_by, "expected_value")
    items.sort(key=lambda c: c.get(key) if c.get(key) is not None else -1,
               reverse=sort_order != "asc")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([cn for _, cn in _EXPORT_COLUMNS])
    for c in items:
        row = []
        for field, _ in _EXPORT_COLUMNS:
            v = c.get(field)
            if field == "value_tier":
                v = _TIER_CN.get(v, v)
            elif field == "channel":
                v = risk_scoring.CHANNEL_LABELS.get(v, v)
            elif field == "risk_factors":
                v = " / ".join(v) if isinstance(v, list) else (v or "")
            elif field == "probability" and v is not None:
                v = f"{v * 100:.1f}%"
            elif field == "expected_value" and v is not None:
                v = f"{v:.0f}"
            row.append("" if v is None else v)
        w.writerow(row)

    # utf-8-sig：Excel 识别 BOM 才会按 UTF-8 解析，否则中文乱码
    data = buf.getvalue().encode("utf-8-sig")
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="customers.csv"'},
    )


@router.get("/{customer_id}")
async def get_customer(customer_id: str, db: Session = Depends(get_db)):
    """单客户详情 —— 与列表/矩阵同源，不重新推理，保证数字一致。"""
    detail = get_customer_detail(db, customer_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"客户 {customer_id} 不存在或模型未训练")
    return detail
