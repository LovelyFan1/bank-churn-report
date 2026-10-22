"""工单写操作的确定性边界：目标归属、操作语义、一次性确认。"""
import hashlib
import json
import re
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import Column, Float, Integer, String, Text

from app.database import Base
from app.models.work_order import WorkOrder
from app.agent import context as ctx_mod

ACTIVE = ("pending", "in_progress")
ORDER_ACTIONS = ("update_work_order", "delete_work_order")


class PendingAgentAction(Base):
    __tablename__ = "pending_agent_actions"
    token_hash = Column(String(64), primary_key=True)
    username = Column(String, nullable=False)
    action = Column(String, nullable=False)
    payload = Column(Text, nullable=False)
    session_id = Column(String(64))
    snapshot = Column(Text)
    expires_at = Column(Float, nullable=False)
    consumed = Column(Integer, nullable=False, default=0)


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    username = Column(String, primary_key=True)
    session_id = Column(String(64), primary_key=True)
    context = Column(Text, nullable=False, default="{}")
    version = Column(Integer, nullable=False, default=0)
    expires_at = Column(Float, nullable=False)
    busy_until = Column(Float, nullable=False, default=0)


def customer_snapshot(db, customer_id):
    from app.services.customer_service import get_retention_customer
    from app.services import risk_scoring
    d = get_retention_customer(db, customer_id)
    info = risk_scoring.get_risk_info()
    if info.get("model_version") and info["model_version"] != risk_scoring._read_model_version():
        raise HTTPException(409, "模型正在更新，请重新查询后准备方案")
    data = {k: d.get(k) for k in (
        "customer_id", "surname", "geography", "probability", "balance", "risk_level",
        "risk_factors", "value_tier", "expected_value", "channel", "action", "exited",
        "has_active_order")}
    data.update(model_used=info.get("model"), thresholds=info.get("thresholds"),
                model_version=risk_scoring._read_model_version())
    return d, hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def operation(question):
    """只识别操作种类；否定/咨询语句不自动生成操作。"""
    from app.agent.guard_rules import write_intents
    intents = write_intents(question)
    if not re.search(r"工单|这张单|这个单|那张单|跟进|C\d{4,}|#\s*\d+", question, re.I):
        return None
    if "delete" in intents and len(intents) == 1:
        return "delete"
    if "cancel" in intents and len(intents) == 1:
        return "cancel"
    return None


def target(question, context):
    """目标只取用户显式编号或上一轮焦点，绝不由本轮宽泛查询扩充。"""
    ids = ctx_mod.explicit_ids(question)
    oids = {int(m.group(1) or m.group(2) or m.group(3)) for m in re.finditer(
        r"#\s*(\d+)|工单\s*(\d+)|(?<![A-Za-z0-9])(\d+)\s*号工单", question)}
    if len(ids) > 1 or len(oids) > 1:
        raise ValueError("请逐张操作，并明确一个客户编号或工单编号。")
    if re.search(r"全部|所有|这些|批量", question):
        raise ValueError("批量取消请先查询工单列表，使用列表中的确认面板逐项核对；不会自动选择工单。")
    if ids or oids:
        return (ids[0].upper() if ids else None, next(iter(oids), None))
    ctx = ctx_mod.normalize(context)
    if ctx.get("focus_customer_id"):
        return ctx["focus_customer_id"], None
    if len(ctx["orders"]) == 1:
        o = ctx["orders"][0]
        return o.get("customer_id") or None, o["order_id"]
    if not ctx["orders"] and len(ctx["customers"]) == 1:
        return ctx["customers"][0]["id"], None
    raise ValueError("当前对象不唯一，请指定客户编号（如 C071081）或工单编号。")


def resolve(db, user, question, context, *, cancel=False):
    from app.routers.work_orders import _visible_orders_query, _assert_can_touch
    cid, oid = target(question, context)
    q = _visible_orders_query(db, user)
    if cid:
        q = q.filter(WorkOrder.customer_id == cid)
    if oid:
        q = q.filter(WorkOrder.id == oid)
    elif cancel:
        q = q.filter(WorkOrder.status.in_(ACTIVE))
    rows = q.limit(2).all()
    if not rows:
        raise ValueError("未找到该客户可操作的工单" + ("（仅待处理、处理中可取消）。" if cancel else "。"))
    if len(rows) != 1:
        raise ValueError("该客户有多张工单，请指定工单编号；不会自动挑选其中一张。")
    order = rows[0]
    _assert_can_touch(user, order, db)
    if cancel and order.status not in ACTIVE:
        raise ValueError("只允许取消待处理或处理中的工单；已结案工单不能取消。")
    return order


def proposal(order, *, delete=False):
    return {
        "__pending_action__": True,
        "action": "delete_work_order" if delete else "update_work_order",
        "summary": (f"{'永久删除' if delete else '取消'}工单 #{order.id} —— "
                    f"客户 {order.customer_id} {order.customer_name}"
                    + ("，删除不可恢复" if delete else "，保留工单与审计记录")),
        "payload": {"order_id": order.id, "customer_id": order.customer_id,
                    **({} if delete else {"status": "cancelled"})},
        "current": {k: getattr(order, k) for k in (
            "customer_id", "customer_name", "status", "assignee", "risk_level")},
        "requires_confirmation": True,
    }


def fingerprint(order):
    # 校验整个快照（包括改派/备注），不能只比较状态。
    raw = {c.name: getattr(order, c.name) for c in WorkOrder.__table__.columns}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()


def issue(db, user, pending, *, session_id=None):
    from app.routers.work_orders import _assert_can_touch
    lease = db.info.get("agent_session_lease")
    if session_id is not None and lease:
        sid, version, deadline = lease
        checked = db.query(AgentSession).filter(
            AgentSession.username == user.username, AgentSession.session_id == sid,
            AgentSession.session_id == session_id, AgentSession.version == version,
            AgentSession.busy_until == deadline, AgentSession.busy_until > time.time(),
        ).update({"busy_until": deadline}, synchronize_session=False)
        if checked != 1:
            raise HTTPException(409, "会话请求已失效，请重新准备操作。")
    action, body = pending["action"], pending["payload"]
    snap = None
    if action in ORDER_ACTIONS:
        order = db.get(WorkOrder, body.get("order_id"))
        if not order or order.customer_id != body.get("customer_id"):
            raise HTTPException(409, "工单与目标客户不匹配，请重新查询。")
        _assert_can_touch(user, order, db)
        if any(getattr(order, k, None) != v for k, v in (pending.get("current") or {}).items()):
            raise HTTPException(409, "工单已变化，请重新查询后确认。")
        if body.get("status") == "cancelled" and order.status not in ACTIVE:
            raise HTTPException(409, "该工单不是待处理或处理中状态，不能取消。")
        snap = fingerprint(order)
    elif action == "create_work_order":
        from app.models.user import User
        from app.services import risk_scoring
        d, snap = customer_snapshot(db, body.get("customer_id"))
        if d.get("has_active_order"):
            raise HTTPException(409, "该客户已有在途工单")
        owner = (body.get("assignee") or user.username).strip()
        if owner in {"我", "本人", "我本人", "自己", "我自己", "当前用户", "当前登录人"}:
            owner = user.username
        from app.services.assignee_identity import resolve as resolve_owner
        selected_owner = resolve_owner(db, owner)
        body.update(customer_name=d["surname"], geography=d["geography"],
            probability=d["probability"], balance=d["balance"], risk_level=d["risk_level"],
            risk_factors=d.get("risk_factors", []), assignee=selected_owner.username,
            strategy=d.get("action") or "", channel=d.get("channel"),
            value_tier_snapshot=d["value_tier"], expected_value_snapshot=d["expected_value"],
            model_used=risk_scoring.get_risk_info().get("model"),
            thresholds_snapshot=risk_scoring.get_risk_info().get("thresholds"))
    token = secrets.token_urlsafe(32)
    db.add(PendingAgentAction(
        token_hash=hashlib.sha256(token.encode()).hexdigest(), username=user.username,
        action=action, payload=json.dumps(body, ensure_ascii=False), snapshot=snap,
        session_id=session_id, expires_at=time.time() + 600, consumed=0))
    db.commit()
    pending["confirmation_token"] = token
    return pending


def claim(db, user, request):
    """先原子占用凭据，再在同一写事务内检查快照；执行失败必须 rollback。"""
    token = request.get("confirmation_token")
    if not isinstance(token, str) or not token or len(token) > 128:
        raise HTTPException(409, "缺少有效确认凭据，请重新发起操作（旧按钮已失效）。")
    key = hashlib.sha256(token.encode()).hexdigest()
    count = db.query(PendingAgentAction).filter(
        PendingAgentAction.token_hash == key,
        PendingAgentAction.username == user.username,
        PendingAgentAction.consumed == 0,
        PendingAgentAction.expires_at > time.time(),
    ).update({"consumed": 1}, synchronize_session=False)
    if count != 1:
        raise HTTPException(409, "确认凭据已使用、已过期或不属于当前用户，请重新查询。")
    item = db.get(PendingAgentAction, key)
    body = json.loads(item.payload)
    if request.get("action") != item.action or request.get("payload") != body:
        raise HTTPException(409, "确认内容与待确认操作不一致，请重新查询。")
    session_id = request.get("session_id")
    if item.session_id is None:
        if session_id is not None or request.get("session_version") is not None:
            raise HTTPException(409, "历史未绑定凭据不能转入当前会话，请重新准备操作。")
    else:
        if session_id != item.session_id:
            raise HTTPException(409, "确认凭据不属于当前会话，请重新准备操作。")
        version = request.get("session_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise HTTPException(409, "缺少有效会话版本，请刷新会话后重试。")
        now = time.time()
        # 与凭据、业务写入和审计处于同一个事务；并发旧窗口不能覆盖上下文。
        changed = db.query(AgentSession).filter(
            AgentSession.username == user.username,
            AgentSession.session_id == session_id,
            AgentSession.version == version,
            AgentSession.expires_at > now,
            AgentSession.busy_until <= now,
        ).update({"version": version + 1}, synchronize_session=False)
        if changed != 1:
            raise HTTPException(409, "会话已更新、已过期或正在处理请求，请刷新后重新确认。")
        db.expire_all()
    if item.action in ORDER_ACTIONS:
        from app.routers.work_orders import _assert_can_touch
        order = db.get(WorkOrder, body["order_id"])
        if not order or fingerprint(order) != item.snapshot:
            raise HTTPException(409, "工单已变化或不存在，请重新查询后确认。")
        _assert_can_touch(user, order, db)
    elif item.action == "create_work_order":
        _, current = customer_snapshot(db, body["customer_id"])
        if current != item.snapshot:
            raise HTTPException(409, "客户或模型快照已变化，请重新准备建单方案")
    return item.action, body
