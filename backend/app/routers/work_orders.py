"""工单管理 Router — CRUD + 统计"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.work_order import WorkOrder
from app.services import risk_scoring
from app.schemas.work_order import (
    WorkOrderCreate,
    WorkOrderUpdate,
    WorkOrderResponse,
)

router = APIRouter(prefix="/api/work-orders", tags=["WorkOrders"])


# ── 辅助函数 ───────────────────────────────────────────

def _order_to_response(order: WorkOrder) -> dict:
    """将 ORM 对象转为响应 dict（JSON 字符串字段 → 原生类型）"""
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
        # 分级依据快照（可能为空 —— 快照功能上线前建的工单没有）
        "thresholds_snapshot": _parse_json(order.thresholds_snapshot, None),
        "model_used": order.model_used,
        # 价值层快照（同上，老工单为空）
        "value_tier_snapshot": order.value_tier_snapshot,
        "expected_value_snapshot": order.expected_value_snapshot,
        # 渠道与覆盖留痕
        "channel": order.channel,
        "channel_overridden": order.channel_overridden or 0,
        "override_reason": order.override_reason,
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
    # 负责人清单 —— 供工单页的「按人筛选」下拉。只列实际指派过的（去重、去空）。
    assignees = [
        r[0] for r in db.query(WorkOrder.assignee)
        .filter(WorkOrder.assignee.isnot(None), WorkOrder.assignee != "")
        .distinct().all()
    ]
    return {
        "total": sum(stats.values()),
        "pending": stats.get("pending", 0),
        "in_progress": stats.get("in_progress", 0),
        "completed": stats.get("completed", 0),
        "lost": stats.get("lost", 0),
        "assignees": sorted(assignees),
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

    # 分级依据快照 —— 前端未传时由后端补齐。
    # 必须用「当前引擎」的阈值，因为它就是判定 body.risk_level 的那套口径
    # （前端从 /api/customers 的 risk 字段拿到的也是同一来源）。
    thresholds = body.thresholds_snapshot
    model_used = body.model_used
    if thresholds is None or model_used is None:
        info = risk_scoring.get_risk_info()
        thresholds = thresholds if thresholds is not None else info.get("thresholds")
        model_used = model_used if model_used is not None else info.get("model")

    # 价值层快照 —— 与等级快照同理，缺省时由后端按同一套规则补齐。
    # 用 body.balance 而非查库：建单传的就是当时的余额，与显示给用户的一致。
    value_tier = body.value_tier_snapshot or risk_scoring.value_tier(body.balance)
    exp_value = (
        body.expected_value_snapshot
        if body.expected_value_snapshot is not None
        else risk_scoring.expected_value(body.probability, body.balance)
    )

    # 渠道 —— 由价值层硬定推荐值，允许人工覆盖（如余额为 0 但资产在他行的客户）。
    # 覆盖必须留原因：否则事后无法解释「为什么给零余额客户派了客户经理」。
    rec = risk_scoring.recommend_action(value_tier, body.risk_level, body.risk_factors)
    channel = body.channel or rec["channel"]
    overridden = int(channel != rec["channel"])
    if overridden and not (body.override_reason or "").strip():
        raise HTTPException(
            status_code=422,
            detail=(
                f"客户属「{value_tier}」价值层，推荐渠道为「"
                f"{risk_scoring.CHANNEL_LABELS[rec['channel']]}」。"
                f"改为「{risk_scoring.CHANNEL_LABELS.get(channel, channel)}」"
                f"时必须填写覆盖原因。"
            ),
        )

    # 动作必须与**最终渠道**匹配，否则会出现「渠道写客户经理、动作写 APP 推送」
    # 这种新的自相矛盾（覆盖渠道后若沿用原动作就会如此）。
    # 覆盖渠道时按该渠道重新取动作；前端传了 strategy 则以它为准。
    action = body.strategy or ""
    if not action or overridden:
        action = risk_scoring.action_for_channel(channel, body.risk_level)

    order = WorkOrder(
        customer_id=body.customer_id,
        customer_name=body.customer_name,
        geography=body.geography,
        risk_level=body.risk_level,
        probability=body.probability,
        balance=body.balance,
        risk_factors=json.dumps(body.risk_factors, ensure_ascii=False),
        strategy=action,
        status="pending",
        assignee=body.assignee,
        note=body.note,
        thresholds_snapshot=json.dumps(thresholds, ensure_ascii=False) if thresholds else None,
        model_used=model_used,
        value_tier_snapshot=value_tier,
        expected_value_snapshot=exp_value,
        channel=channel,
        channel_overridden=overridden,
        override_reason=(body.override_reason or None) if overridden else None,
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
    #
    # ⚠ 这里必须处理「回退」这一支，否则会留下**自相矛盾的脏数据**。
    #   实测（修复前）：PUT {status:'in_progress'} 到一条 completed 工单上，
    #   返回 200 且 status 变成 in_progress，但 result 仍是 'retained'、
    #   completed_at 仍带着旧时间戳 —— 一条"处理中却已记录完成时间与结果"的工单。
    #   后果：按 result 聚合的挽留效果报表会把它算成"已挽回"，
    #   而客户名单/工单列表显示它还在处理中。两个页面互相打架。
    #
    #   规则：状态离开结案态（completed / lost）时，结案痕迹必须一并清除。
    #   注：WorkOrderUpdate 不含 completed_at 字段，故它不会被请求携带，
    #   回退时直接置 None 即可；result 则可能被显式传入（前端会传 null 来清空），
    #   因此仅在请求**未提供** result 时才由后端补默认值。
    if "status" in update_data:
        new_status = update_data["status"]

        if new_status in ("completed", "lost"):
            # 结案：result 缺省时按状态补（completed→retained，lost→lost）
            if not update_data.get("result"):
                update_data["result"] = "retained" if new_status == "completed" else "lost"
            update_data["completed_at"] = datetime.now(timezone.utc)
        else:
            # 非结案态（pending / in_progress）：清除结案痕迹
            if "result" not in update_data:
                update_data["result"] = None
            update_data["completed_at"] = None

    # 人工调整 risk_level 时，同步刷新分级依据快照与**动作** ——
    # 否则会出现「等级是新的、依据是旧的」这种更隐蔽的不一致。
    if "risk_level" in update_data:
        info = risk_scoring.get_risk_info()
        if info.get("thresholds"):
            update_data["thresholds_snapshot"] = json.dumps(
                info["thresholds"], ensure_ascii=False
            )
        if info.get("model"):
            update_data["model_used"] = info["model"]

        # 动作同样依赖等级（动作 = f(价值层, 等级)），等级改了动作必须重算，
        # 否则会出现「等级=极高、动作还是中等档的」——与上面同理的不一致。
        # 价值层取工单自身余额（余额建单即冻结，PUT 不改它）。
        # 仅当调用方没有显式指定 strategy 时才自动重算，保留人工微调的余地。
        if not update_data.get("strategy"):
            tier = order.value_tier_snapshot or risk_scoring.value_tier(order.balance)
            # 渠道可能被人工覆盖过，按其实际渠道取动作，避免与渠道再次打架
            ch = order.channel or risk_scoring.CHANNEL_BY_TIER[tier]
            recalc = risk_scoring.action_for_channel(ch, update_data["risk_level"])
            if recalc:
                update_data["strategy"] = recalc

    for field, value in update_data.items():
        setattr(order, field, value)

    order.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)

    # GET 详情 / PUT 更新 都返回工单本身，不是响应契约，不需要 Pydantic 校验；
    # WorkOrderResponse 保留供未来加 response_model 时使用。
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
