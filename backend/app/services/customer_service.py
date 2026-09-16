"""客户列表服务 — 复用 risk_scoring 打分，负责筛选/排序/分页/汇总。

打分（概率校准 + 分级）统一由 risk_scoring 提供，本服务只做列表逻辑。
"""

from sqlalchemy.orm import Session

from app.models.work_order import WorkOrder
from app.services import risk_scoring


def _active_order_ids(db: Session) -> set:
    """有进行中工单（pending / in_progress）的客户编号集合。"""
    rows = (
        db.query(WorkOrder.customer_id)
        .filter(WorkOrder.status.in_(["pending", "in_progress"]))
        .all()
    )
    return {r[0] for r in rows}


def list_customers(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    geography: str | None = None,
    risk_level: str | None = None,
    value_tier: str | None = None,
    exited: int | None = None,
    has_order: bool | None = None,
    sort_by: str = "probability",
    sort_order: str = "desc",
) -> dict:
    scored = risk_scoring.get_scored_customers(db)
    risk_info = risk_scoring.get_risk_info()

    if scored is None:
        return {
            "items": [], "total": 0, "page": page, "page_size": page_size,
            "total_pages": 0, "model_used": None,
            "summary": {"total_customers": 0, "high_risk": 0, "exited": 0, "active_orders": 0},
            "risk": risk_info,
            "error": "模型尚未训练，请先调用 POST /api/model/train",
        }

    # 筛选 —— 与导出共用同一实现，避免两处各写一套导致「导出的和列表看到的不一致」
    items, summary = _filtered_customers(
        db, search=search, geography=geography, risk_level=risk_level,
        value_tier=value_tier, exited=exited, has_order=has_order,
    )

    # 排序
    key_map = {
        "probability": "probability",
        "balance": "balance",
        "age": "age",
        "credit_score": "credit_score",
        # 期望价值 = 概率 × 余额，用于按「值得投入多少」排序干预优先级。
        # 默认仍是 probability，不改变既有行为；调用方需显式指定才切换。
        "expected_value": "expected_value",
    }
    key = key_map.get(sort_by, "probability")
    reverse = sort_order != "asc"
    items.sort(key=lambda c: c.get(key) if c.get(key) is not None else -1, reverse=reverse)

    # 分页
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 0
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]

    return {
        "items": page_items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "model_used": risk_info.get("model"),
        "summary": summary,
        "risk": risk_info,
    }


def _filtered_customers(
    db: Session,
    search: str | None = None,
    geography: str | None = None,
    risk_level: str | None = None,
    value_tier: str | None = None,
    exited: int | None = None,
    has_order: bool | None = None,
) -> tuple[list[dict], dict]:
    """按筛选条件返回全量客户（不分页）。供导出复用，避免与 list_customers 各写一套。

    返回 (items, summary)。
    """
    scored = risk_scoring.get_scored_customers(db)
    if scored is None:
        return [], {}

    active_ids = _active_order_ids(db)
    items = []
    for c in scored:
        d = dict(c)
        d["has_active_order"] = d["customer_id"] in active_ids
        items.append(d)

    if search:
        s = search.strip().lower()
        items = [c for c in items if s in c["customer_id"].lower() or s in c["surname"].lower()]
    if geography:
        items = [c for c in items if c["geography"] == geography]
    if risk_level:
        items = [c for c in items if c["risk_level"] == risk_level.upper()]
    if value_tier:
        items = [c for c in items if c["value_tier"] == value_tier.upper()]
    if exited is not None:
        items = [c for c in items if c["exited"] == exited]
    if has_order is not None:
        items = [c for c in items if c["has_active_order"] == has_order]

    # 四个等级的完整分布 —— 供数据概览页的「风险等级分布」饼图使用。
    # 此前饼图的数据来自 batch-score 的 risk_distribution，但那个任务会把
    # 10 万行重新打一遍分（实测 12~13 秒）才能得到这四个数，而这里
    # `scored` 已经是带 risk_level 的全量打分结果 —— 直接统计即可，零额外计算。
    risk_distribution = {
        lvl: sum(1 for c in scored if c["risk_level"] == lvl)
        for lvl in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    }
    summary = {
        "total_customers": len(scored),
        "high_risk": risk_distribution["CRITICAL"] + risk_distribution["HIGH"],
        "exited": sum(1 for c in scored if c["exited"] == 1),
        "active_orders": len(active_ids),
        # 注意是**全量**分布，不受上方筛选条件影响 —— 与它并列的
        # total_customers/exited 也都是全量口径，保持一致。
        "risk_distribution": risk_distribution,
    }
    return items, summary


def get_customer_detail(db: Session, customer_id: str) -> dict | None:
    """单个客户详情。

    直接从全量打分结果里取（该结果已带 probability/risk_level/value_tier/
    expected_value/channel/action/reason/risk_factors），**不重新推理** ——
    保证详情页与列表页、矩阵页的数字完全一致，不会出现"点进去变了"。
    """
    scored = risk_scoring.get_scored_customers(db)
    if scored is None:
        return None
    for c in scored:
        if c["customer_id"] == customer_id:
            d = dict(c)
            d["has_active_order"] = c["customer_id"] in _active_order_ids(db)
            return d
    return None
