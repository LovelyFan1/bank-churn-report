"""工单管理 Router — CRUD + 统计"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.work_order import WorkOrder
from app.schemas.work_order import (
    WorkOrderCreate,
    WorkOrderUpdate,
    WorkOrderResponse,
)

router = APIRouter(prefix="/api/work-orders", tags=["WorkOrders"])


# ── 辅助函数 ───────────────────────────────────────────

def _order_to_response(order: WorkOrder) -> dict:
    """将 ORM 对象转为响应 dict（risk_factors JSON → list）"""
    data = {
        "id": order.id,
        "customer_id": order.customer_id,
        "customer_name": order.customer_name,
        "geography": order.geography,
        "risk_level": order.risk_level,
        "probability": order.probability,
        "balance": order.balance,
        "risk_factors": _parse_json(order.risk_factors, []),
        "strategy": order.strategy,
        "status": order.status,
        "result": order.result,
        "assignee": order.assignee,
        "note": order.note,
        "created_at": order.created_at,
        "updated_at": order.updated_at,
        "completed_at": order.completed_at,
    }
    return data


def _parse_json(raw: str | None, default):
    """安全解析 JSON 字符串"""
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


# ── 统计 ───────────────────────────────────────────────

@router.get("/stats")
async def get_stats(db: Session = Depends(get_db)):
    """各状态工单数量统计"""
    from sqlalchemy import func

    rows = (
        db.query(WorkOrder.status, func.count(WorkOrder.id))
        .group_by(WorkOrder.status)
        .all()
    )
    stats = {row[0]: row[1] for row in rows}
    return {
        "total": sum(stats.values()),
        "pending": stats.get("pending", 0),
        "in_progress": stats.get("in_progress", 0),
        "completed": stats.get("completed", 0),
        "lost": stats.get("lost", 0),
    }


# ── 列表查询 ───────────────────────────────────────────

@router.get("")
async def list_work_orders(
    status: str | None = Query(default=None, description="按状态筛选: pending / in_progress / completed / lost"),
    risk_level: str | None = Query(default=None, description="按风险等级筛选"),
    assignee: str | None = Query(default=None, description="按负责人筛选"),
    search: str | None = Query(default=None, description="搜索客户姓名 / 编号"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """工单列表 - 支持筛选、搜索、分页"""
    q = db.query(WorkOrder)

    if status:
        q = q.filter(WorkOrder.status == status)
    if risk_level:
        q = q.filter(WorkOrder.risk_level == risk_level.upper())
    if assignee:
        q = q.filter(WorkOrder.assignee.ilike(f"%{assignee}%"))
    if search:
        q = q.filter(
            WorkOrder.customer_name.ilike(f"%{search}%")
            | WorkOrder.customer_id.ilike(f"%{search}%")
        )

    total = q.count()
    total_pages = max(1, (total + page_size - 1) // page_size)

    orders = (
        q.order_by(WorkOrder.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "items": [_order_to_response(o) for o in orders],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


# ── 活跃客户查询（用于前端按钮状态）───────────────────

@router.get("/active-customers")
async def get_active_customers(db: Session = Depends(get_db)):
    """返回所有有进行中工单的客户 ID 列表"""
    orders = (
        db.query(WorkOrder.customer_id)
        .filter(WorkOrder.status.in_(["pending", "in_progress"]))
        .all()
    )
    return {"customer_ids": [o[0] for o in orders]}


# ── 创建工单 ───────────────────────────────────────────

@router.post("", status_code=201)
async def create_work_order(body: WorkOrderCreate, db: Session = Depends(get_db)):
    """创建新工单"""
    # 互斥检查：同一客户已有进行中的工单则拒绝
    existing = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.customer_id == body.customer_id,
            WorkOrder.status.in_(["pending", "in_progress"]),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"客户 {body.customer_name} 已有进行中的工单（#{existing.id}），请先处理完毕再创建新工单",
        )

    now = datetime.now(timezone.utc)
    order = WorkOrder(
        customer_id=body.customer_id,
        customer_name=body.customer_name,
        geography=body.geography,
        risk_level=body.risk_level,
        probability=body.probability,
        balance=body.balance,
        risk_factors=json.dumps(body.risk_factors, ensure_ascii=False),
        strategy=body.strategy,
        status="pending",
        assignee=body.assignee,
        note=body.note,
        created_at=now,
        updated_at=now,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return _order_to_response(order)


# ── 工单详情 ───────────────────────────────────────────

@router.get("/{order_id}")
async def get_work_order(order_id: int, db: Session = Depends(get_db)):
    """获取单个工单详情"""
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    return _order_to_response(order)


# ── 更新工单 ───────────────────────────────────────────

@router.put("/{order_id}")
async def update_work_order(order_id: int, body: WorkOrderUpdate, db: Session = Depends(get_db)):
    """更新工单状态、负责人、备注等"""
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")

    update_data = body.model_dump(exclude_unset=True)

    # 状态流转时自动处理 result 和 completed_at
    if "status" in update_data:
        new_status = update_data["status"]
        if new_status == "completed" and not update_data.get("result"):
            update_data["result"] = "retained"
        if new_status == "lost" and not update_data.get("result"):
            update_data["result"] = "lost"
        if new_status in ("completed", "lost"):
            update_data["completed_at"] = datetime.now(timezone.utc)

    for field, value in update_data.items():
        setattr(order, field, value)

    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return _order_to_response(order)


# ── 删除工单 ───────────────────────────────────────────

@router.delete("/{order_id}", status_code=204)
async def delete_work_order(order_id: int, db: Session = Depends(get_db)):
    """删除工单"""
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    db.delete(order)
    db.commit()
    return None
