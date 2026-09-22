"""工单管理 Router — CRUD + 统计"""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.models.work_order import WorkOrder
from app.services import auth_deps
from app.services import auth_service as auth
from app.services import privacy
from app.services import risk_scoring
from app.schemas.work_order import (
    WorkOrderCreate,
    WorkOrderUpdate,
    WorkOrderResponse,
    WorkOrderBatchCreate,
)

router = APIRouter(prefix="/api/work-orders", tags=["WorkOrders"])


# ── 数据级权限（工单归属）────────────────────────────────
#
# ⚠ 为什么权限点不够，还要在路由里判：
#   RBAC 的权限点是**功能级**的（"能不能改工单"），表达不了
#   "只能改**自己的**工单"这种**数据级**限制。staff 有 order:assigned
#   却没有 order:write，但仍必须再判一次归属 —— 否则他拿到任意工单 id
#   就能推进别人的单（实测这类越权不会有任何报错）。
#
#   这是 4A 里"授权"从粗到细的第二个层次：先看角色，再看对象。

def _is_manager_level(user: User) -> bool:
    """是否具备「派单方」能力（能建单 / 改任意单 / 删单）。"""
    return auth.has_perm(user.role or "", auth.PERM_ORDER_WRITE)


def _own_identifiers(user: User) -> set[str]:
    """本人在工单里可能出现的 assignee 取值。

    ⚠ 必须同时匹配**工号与姓名**：改造前建的历史工单里存的是姓名
      （如"王晓芸"），改造后存工号（如"wangxiaoyun"）。只认一种的话，
      专员登录后会看不到自己名下的历史工单 —— 表现为"我的工单是空的"。
    """
    s = {user.username or ""}
    if user.display_name:
        s.add(user.display_name)
    s.discard("")
    return s


def _assert_can_touch(user: User, order: WorkOrder) -> None:
    """数据级授权：能否操作这张工单。

    经理及以上：任意工单。
    专员（order:assigned）：仅指派给自己的工单。
    其他角色：无权（由接口上的 require_perm 拦在前面，这里是双保险）。
    """
    if _is_manager_level(user):
        return
    if not auth.has_perm(user.role or "", auth.PERM_ORDER_ASSIGNED):
        raise HTTPException(
            status_code=403,
            detail=f"当前角色（{auth.ROLE_LABELS.get(user.role, user.role)}）无权操作工单",
        )
    mine = _own_identifiers(user)
    if (order.assignee or "") not in mine:
        raise HTTPException(
            status_code=403,
            detail="该工单未指派给你，无法操作（仅可处理指派给自己的工单）",
        )


def _visible_orders_query(db: Session, user: User):
    """按可见范围过滤工单查询。

    专员只看自己的；其余角色看全部（viewer 另有脱敏，见 list 的说明）。
    """
    q = db.query(WorkOrder)
    if _is_manager_level(user):
        return q
    if auth.has_perm(user.role or "", auth.PERM_ORDER_ASSIGNED):
        return q.filter(WorkOrder.assignee.in_(list(_own_identifiers(user))))
    return q


def _valid_assignee_values(db: Session) -> set[str]:
    """可被指派的有效取值集合：所有启用账号的**工号 + 姓名**。

    ⚠ 为什么同时收姓名：历史工单里存的是姓名（如"王晓芸"）。
      PUT 一张老工单时会带上原 assignee，若只认工号就会被自己的校验拒掉。
      这是向后兼容，不是"允许乱填" —— 只有真实存在的账号姓名才在集合里。
    """
    vals: set[str] = set()
    for username, display_name in db.query(User.username, User.display_name) \
            .filter(User.is_active == 1).all():
        if username:
            vals.add(username)
        if display_name:
            vals.add(display_name)
    return vals


def _is_valid_assignee(db: Session, value: str) -> bool:
    """负责人取值是否有效（是某个启用账号的工号或姓名）。"""
    return value in _valid_assignee_values(db)


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
        # 建单人（行员号）+ 负责人（行员号）。前端用 /api/users/assignable
        # 的映射把工号显示成姓名；解析不到则原样显示（历史数据）。
        "created_by": order.created_by,
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
async def get_stats(user: User = Depends(auth_deps.current_user),
                    db: Session = Depends(get_db)):
    """各状态工单数量统计

    ⚠ 统计必须与列表**同一可见范围**：专员若在列表只看到自己的 20 单、
    统计却报全库 60 单，"全部工单"卡片与表格就对不上 —— 用户会以为
    系统坏了。故这里同样按 _visible_orders_query 过滤。
    """
    from sqlalchemy import func

    visible = _visible_orders_query(db, user)
    rows = (
        visible.with_entities(WorkOrder.status, func.count(WorkOrder.id))
        .group_by(WorkOrder.status)
        .all()
    )
    stats = {row[0]: row[1] for row in rows}
    # 负责人清单 —— 供工单页的「按人筛选」下拉。只列实际指派过的（去重、去空）。
    assignees = [
        r[0] for r in visible.with_entities(WorkOrder.assignee)
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
        # 便于前端说明"为什么只看到这些"（专员视角）
        "scoped_to_me": not _is_manager_level(user)
                        and auth.has_perm(user.role or "", auth.PERM_ORDER_ASSIGNED),
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
    user: User = Depends(auth_deps.current_user),
    db: Session = Depends(get_db),
):
    """工单列表 - 支持筛选、搜索、分页

    ⚠ 可见范围（数据级授权）：专员（order:assigned）**只看到指派给自己的**
      工单。经理及以上看全部。这不是脱敏（不隐藏字段），而是根本不返回 ——
      "别人手上有哪些客户"本身就是不该横向可见的信息。

    ⚠ 脱敏：无 `customer:identify` 权限时，工单的**管理属性**（状态、
      负责人、时间、渠道）保留，但客户身份（姓名、编号、余额、概率、
      风险因素、备注）**不返回** —— 见 services/privacy.py。

      备注尤其必须去掉：系统生成的建单理由会逐项复述客户姓名、
      余额与风险因素，留着它等于把脱敏白做。
    """
    q = _visible_orders_query(db, user)

    if status:
        q = q.filter(WorkOrder.status == status)
    if risk_level:
        q = q.filter(WorkOrder.risk_level == risk_level.upper())
    if assignee:
        q = q.filter(WorkOrder.assignee.ilike(f"%{assignee}%"))
    if search:
        # ⚠ 搜索同样受可见范围约束（q 已过滤），不会因搜索越权看到别人的单。
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

    items = [_order_to_response(o) for o in orders]
    masked = not privacy.can_identify(user)
    if masked:
        items = privacy.mask_orders(items)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "masked": masked,
        "mask_notice": privacy.MASK_NOTICE if masked else "",
    }


# ── 活跃客户查询（用于前端按钮状态）───────────────────

@router.get("/active-customers")
async def get_active_customers(user: User = Depends(auth_deps.current_user),
                               db: Session = Depends(get_db)):
    """返回所有有进行中工单的客户 ID 列表

    ⚠ 无 `customer:identify` 权限时返回**空列表**：这份名单本身就是
      96,418 人中"哪些人有在途工单"的精确索引，是一份身份信息。
      前端不依赖它也能工作 —— 列表接口的每一行已带 `has_active_order`，
      该字段在脱敏后仍然保留（它不指向具体是谁）。
    """
    if not privacy.can_identify(user):
        return {"customer_ids": [], "masked": True,
                "mask_notice": privacy.MASK_NOTICE}
    orders = (
        db.query(WorkOrder.customer_id)
        .filter(WorkOrder.status.in_(["pending", "in_progress"]))
        .all()
    )
    return {"customer_ids": [o[0] for o in orders], "masked": False}


# ── 创建工单 ───────────────────────────────────────────

@router.post("", status_code=201)
async def create_work_order(body: WorkOrderCreate,
                            request: Request,
                            user: User = Depends(
                                auth_deps.require_perm(auth.PERM_ORDER_WRITE)),
                            db: Session = Depends(get_db)):
    """创建新工单。

    ⚠ 只有 order:write（经理及以上）能建单。专员（order:assigned）
       **不能建单** —— 派单权在经理手里，这是职责分离（SoD）。

    ⚠ 权限：需要 `order:write`。**这是实测发现的缺口修复** ——
      引入 RBAC 时我只在 Agent 路径（/api/agent/confirm）加了权限检查，
      却漏了页面直接调用的这个接口。后果：只读角色可以绕过页面建单，
      「只读」这个角色形同虚设（实测：`POST /api/work-orders` 对
      chenjie/viewer 返回 201）。

      修法是在**每个写接口**上显式声明权限，而不是指望中间件 ——
      中间件只保证"必须登录"，不区分"能读"与"能写"。
    """
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

    # ── 负责人校验 ──────────────────────────────────────
    #
    # ⚠ 为什么必填：工单是"派活"，没有负责人的工单等于没派出去。
    #   此前 assignee 可空，实测出现 1 条空负责人的工单 ——
    #   在"按负责人筛选/统计工作量"的视角里它是隐形的。
    #
    # ⚠ 为什么校验收在这里（服务端）而不是只在前端：
    #   三个入口（客户页单建、批量、Agent）各自校验必然漏一个，
    #   且直接调接口可绕过。这里做唯一判据。
    #
    # ⚠ 校验**两套取值**（工号 or 姓名），因为历史工单存的是姓名：
    #   若只认工号，把老工单改一下（PUT）就会被自己的校验拒掉。
    assignee = (body.assignee or "").strip()
    if not assignee:
        raise HTTPException(status_code=422, detail="必须指定负责人")
    if not _is_valid_assignee(db, assignee):
        raise HTTPException(
            status_code=422,
            detail=f"负责人「{assignee}」不是有效行员（请从列表中选择）",
        )

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
        # ⚠ 建单人**由服务端从会话取**，绝不读 body.created_by ——
        #   客户端声称的"操作者"可伪造，与审计同一原则。
        created_by=user.username,
        assignee=assignee,
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
    db.flush()                    # 拿到自增 id 供审计引用
    # 审计：页面直接建单此前**没有**留痕（只有 Agent 路径写了）。
    # 引入派单后"谁把哪个客户派给了谁"是必须可查的，故一并补上。
    auth_deps.write_audit(
        db, user=user, action="create_work_order",
        target=str(body.customer_id),
        detail={"order_id": order.id, "assignee": assignee,
                "channel": channel, "via": "ui"},
        source="ui", ip=auth_deps._client_ip(request),
        message=f"为客户 {body.customer_id} 创建挽留工单，负责人 {assignee}",
    )
    db.commit()
    db.refresh(order)
    return _order_to_response(order)


# ── 批量建单 ───────────────────────────────────────────
#
# ⚠ 路由顺序至关重要：本接口必须声明在 `/{order_id}` **之前**。
#   FastAPI 按声明顺序匹配，若放到后面，"batch" 会被 `/{order_id}`
#   当成路径参数吞掉，返回 422（int 解析失败），且错误信息完全不提路由顺序。
#   同类前车之鉴见 app/routers/customers.py 的 /suggested-notes/batch 注释。

@router.post("/batch", status_code=201)
async def create_work_orders_batch(body: WorkOrderBatchCreate,
                                    request: Request,
                                    user: User = Depends(
                                        auth_deps.require_perm(auth.PERM_ORDER_WRITE)),
                                    db: Session = Depends(get_db)):
    """批量建单 —— **每条可独立指定负责人与理由**。

    ⚠ 关键设计：客户画像（姓名/余额/等级/价值层）一律由**服务端按
      customer_id 重新查库**，不接受前端传入。原因见 BatchOrderItem 的说明：
      工单里的快照会被当作"建单时的真实依据"永久留存，若允许客户端声称，
      就等于让人可以往审计凭据里写假数。

    ⚠ 失败**不中断**后续：一条失败不应让其余全部回滚（否则用户要重来）。
      逐条汇报成功/失败/跳过，与 Agent 的 /confirm-batch 行为保持一致 ——
      同一个业务动作在两个入口必须有相同语义。

    返回：
      ok / failed / skipped / created，语义同 /api/agent/confirm-batch。
    """
    from app.services.customer_service import get_customer_detail
    from app.services import note_service

    ok_list: list[dict] = []
    failed: list[dict] = []
    skipped: list[str] = []

    # 去重保序（用户可能重复勾选）
    seen: set[str] = set()
    items = []
    for it in body.items:
        cid = (it.customer_id or "").strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        items.append((cid, it))

    for cid, it in items:
        # 互斥：已有进行中工单则跳过（提前判，比等 409 再解释清楚）
        active = (
            db.query(WorkOrder.id)
            .filter(WorkOrder.customer_id == cid,
                    WorkOrder.status.in_(["pending", "in_progress"]))
            .first()
        )
        if active:
            skipped.append(cid)
            continue

        detail = get_customer_detail(db, cid)
        if detail is None:
            failed.append({"customer_id": cid, "reason": "客户不存在或模型未训练"})
            continue

        # 负责人：留空则默认建单人（经理自己跟单是最常见的起点）
        assignee = (it.assignee or "").strip() or user.username
        # 理由：留空则用系统建议（确定性模板，非 LLM）
        note = it.note or ""
        if not note:
            try:
                note = note_service.build_reason(
                    prob=detail.get("probability", 0.0),
                    balance=detail.get("balance", 0.0),
                    risk_level=detail.get("risk_level", "MEDIUM"),
                    value_tier=detail.get("value_tier", "LOW"),
                    risk_factors=detail.get("risk_factors", []),
                    expected_value=detail.get("expected_value"),
                    action=detail.get("action") or detail.get("strategy"),
                    channel=detail.get("channel"),
                )["note"]
            except Exception:
                pass   # 理由生成失败不阻断建单

        payload = WorkOrderCreate(
            customer_id=detail.get("customer_id"),
            customer_name=detail.get("surname"),
            geography=detail.get("geography"),
            risk_level=detail.get("risk_level") or "MEDIUM",
            probability=detail.get("probability") or 0.0,
            balance=detail.get("balance") or 0.0,
            risk_factors=detail.get("risk_factors") or [],
            strategy=detail.get("action") or detail.get("strategy") or "",
            assignee=assignee,
            note=note,
            # 渠道直接用该客户价值层的推荐值 —— 不覆盖，故无需 override_reason
            channel=detail.get("channel"),
            value_tier_snapshot=detail.get("value_tier"),
            expected_value_snapshot=detail.get("expected_value"),
        )
        try:
            # 复用单建逻辑（含负责人校验、快照补齐、渠道校验、审计），
            # 不另写一套 —— 否则批量与单建的工单会有口径差异
            created = await create_work_order(payload, request, user=user, db=db)
            ok_list.append({
                "id": created.get("id"),
                "customer_id": created.get("customer_id"),
                "customer_name": created.get("customer_name"),
                "assignee": created.get("assignee"),
            })
        except HTTPException as e:
            failed.append({"customer_id": cid, "reason": str(e.detail)})
        except Exception as e:
            failed.append({"customer_id": cid,
                           "reason": f"{type(e).__name__}: {e}"})

    # 批量操作审计（无论成败都留痕）—— 与 Agent 的 confirm-batch 一致
    auth_deps.write_audit(
        db, user=user, action="create_work_order_batch",
        target=f"{len(items)} 位客户",
        detail={"requested": len(items), "succeeded": len(ok_list),
                "failed": len(failed), "skipped": len(skipped),
                "order_ids": [o["id"] for o in ok_list]},
        source="ui", ip=auth_deps._client_ip(request),
        message=(f"批量建单：成功 {len(ok_list)}，失败 {len(failed)}，"
                 f"跳过 {len(skipped)}"),
    )
    db.commit()

    return {
        "requested": len(items),
        "succeeded": len(ok_list),
        "failed_count": len(failed),
        "skipped_count": len(skipped),
        "created": ok_list,
        "failed": failed,
        "skipped": skipped,
    }


# ── 工单详情 ───────────────────────────────────────────

@router.get("/{order_id}")
async def get_work_order(order_id: int,
                         user: User = Depends(auth_deps.current_user),
                         db: Session = Depends(get_db)):
    """获取单个工单详情

    ⚠ 无 `customer:identify` 权限时同样脱敏 —— 否则列表脱敏了、
      点进去却能看到全部客户信息，等于绕过了限制。
    """
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    # 数据级授权：专员只能看指派给自己的单。
    # ⚠ 返回 404 而不是 403：403 等于确认"这张单存在但你不能看"，
    #   会把别人的工单 id 变成可探测的信息。列表已过滤，这里保持一致。
    if not _is_manager_level(user) \
            and (order.assignee or "") not in _own_identifiers(user):
        raise HTTPException(status_code=404, detail="工单不存在")
    data = _order_to_response(order)
    if not privacy.can_identify(user):
        data = privacy.mask_order(data)
        data["masked"] = True
        data["mask_notice"] = privacy.MASK_NOTICE
    else:
        data["masked"] = False
    return data


# ── 更新工单 ───────────────────────────────────────────

@router.put("/{order_id}")
async def update_work_order(order_id: int, body: WorkOrderUpdate,
                            request: Request,
                            user: User = Depends(
                                auth_deps.require_any_perm(
                                    auth.PERM_ORDER_WRITE,
                                    auth.PERM_ORDER_ASSIGNED)),
                            db: Session = Depends(get_db)):
    """更新工单（状态 / 负责人 / 备注 / 等级等）。

    ⚠ 两类主体的权限**不同**（这是本接口最容易出错的地方）：

      经理（order:write）    可改任意工单、任意字段，含改派负责人
      专员（order:assigned） 只能改**指派给自己**的工单，且只能改
                             状态 / 结果 / 备注 —— 不能改派负责人
                             （否则他可以把别人的单改成自己的，或把自己的
                              单甩给别人），也不能改风险等级/概率这类
                              判定字段（那是模型的产出，不是执行结果）。

    ⚠ 校验顺序要紧：先判归属（_assert_can_touch），再判字段白名单。
      反过来的话，专员改别人的单会收到"字段不允许"而非"不是你的单"，
      错误信息指向错误的方向。
    """
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")

    # 数据级授权：专员只能碰自己的单
    _assert_can_touch(user, order)

    update_data = body.model_dump(exclude_unset=True)

    # ── 字段级授权：专员只能改执行结果相关字段 ────────────
    if not _is_manager_level(user):
        STAFF_WRITABLE = {"status", "result", "note"}
        illegal = sorted(set(update_data) - STAFF_WRITABLE)
        if illegal:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"客户专员只能修改：状态 / 处理结果 / 备注。"
                    f"本次请求包含不允许的字段：{', '.join(illegal)}"
                    f"（改派负责人与调整风险等级属于客户经理职责）"
                ),
            )

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

    # 改派负责人时校验取值有效（经理才可能走到这里 —— 专员被字段白名单挡住）
    if "assignee" in update_data:
        new_a = (update_data["assignee"] or "").strip()
        if not new_a:
            raise HTTPException(status_code=422, detail="负责人不能为空（工单必须有人负责）")
        if not _is_valid_assignee(db, new_a):
            raise HTTPException(
                status_code=422,
                detail=f"负责人「{new_a}」不是有效行员（请从列表中选择）",
            )
        update_data["assignee"] = new_a

    for field, value in update_data.items():
        setattr(order, field, value)

    order.updated_at = datetime.now(timezone.utc)
    # 审计：状态流转与改派必须留痕（"谁把这张单改成了什么"）
    auth_deps.write_audit(
        db, user=user, action="update_work_order",
        target=f"#{order_id}",
        detail={"order_id": order_id,
                "customer_id": order.customer_id,
                "changed": sorted(update_data.keys()),
                "assignee": order.assignee,
                "status": order.status},
        source="ui", ip=auth_deps._client_ip(request),
        message=f"更新工单 #{order_id}（{', '.join(sorted(update_data.keys()))}）",
    )
    db.commit()
    db.refresh(order)

    # GET 详情 / PUT 更新 都返回工单本身，不是响应契约，不需要 Pydantic 校验；
    # WorkOrderResponse 保留供未来加 response_model 时使用。
    return _order_to_response(order)


# ── 删除工单 ───────────────────────────────────────────

@router.delete("/{order_id}", status_code=204)
async def delete_work_order(order_id: int,
                            request: Request,
                            user: User = Depends(
                                auth_deps.require_perm(auth.PERM_ORDER_WRITE)),
                            db: Session = Depends(get_db)):
    """删除工单（需 `order:write`）。

    ⚠ 只给 order:write（经理及以上）—— 专员**不能删单**。
      删除不可逆，若执行岗能删，一次误操作就可能抹掉别人名下的在途工单，
      且"谁删的"事后只能靠审计倒查。权限检查尤其必要：
      实测中只读角色曾能删除工单（见 create 的说明）。
    """
    order = db.query(WorkOrder).filter(WorkOrder.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="工单不存在")
    # 审计先写、后删 —— 删除后工单信息就没了，必须先留痕
    auth_deps.write_audit(
        db, user=user, action="delete_work_order",
        target=f"#{order_id}",
        detail={"order_id": order_id, "customer_id": order.customer_id,
                "assignee": order.assignee, "status": order.status},
        source="ui", ip=auth_deps._client_ip(request),
        message=f"删除工单 #{order_id}（客户 {order.customer_id}）",
    )
    db.delete(order)
    db.commit()
    return None
