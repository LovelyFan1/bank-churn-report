"""智能体 Router —— 对话式任务型 Agent 的对外接口。

**与其它 router 的区别**

其它 router 都是"查询 → 返回结构化 JSON"。本 router 是**任务型**：
输入一句自然语言，输出一段回答 + 依据 + （可能的）待确认动作。

**为什么把"缺 key/缺依赖"做成 503 而不是降级**

若 Agent 不可用时静默退回规则匹配，调用方会以为「大模型在回答」，
实际没有 —— 属于最坏的一类不一致（本项目反复强调的问题）。
故此处明确返回 503 + 原因，前端据此如实提示。
"""

import logging
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.agent import guard_rules, llm, order_safety
from app.agent import context as ctx_mod
SESSION_TTL_SECONDS = 3600
from app.database import get_db
from app.models.user import User
from app.services import auth_deps
from app.services import auth_service as auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["Agent"])


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=800,
                          description="用户的自然语言问题")
    session_id: str = Field(default="default", max_length=64,
                            description="按登录用户隔离的服务端会话标识")
    session_version: int | None = Field(default=None, ge=0)
    # 数据库保存按用户隔离的权威任务状态，跨worker共享版本和短期租约。
    # 仅首次兼容旧前端实体指针；客户端数值不作为事实证据。
    # Any允许normalize裁剪损坏的旧上下文，避免兼容输入使整轮对话422。
    context: Any = Field(default=None,
                         description="上一轮返回的会话上下文，原样回传")


@router.get("/health")
async def agent_health():
    """Agent 可用性自检 —— 供前端决定是否显示对话入口。

    返回 available=false 时带上具体原因（缺 key / 缺依赖 / 被关闭），
    而不是笼统的"不可用"。
    """
    ok, reason = llm.is_available()
    return {
        "session_policy": {"context_ttl_seconds": SESSION_TTL_SECONDS,
                           "confirmation_ttl_seconds": 600, "batch_limit": 20, "query_limit": 50},
        "available": ok,
        "reason": reason,
        "model": llm.settings.AGENT_LLM_MODEL if ok else None,
        "base_url": llm.settings.AGENT_LLM_BASE_URL if ok else None,
        "max_tool_rounds": llm.settings.AGENT_MAX_TOOL_ROUNDS,
    }


@router.get("/capabilities")
async def agent_capabilities():
    """Agent 的能力边界 —— 能做什么、不能做什么。

    ⚠ 主动暴露"答不了什么"，比被问到时支吾更有价值：
      它把系统的**边界**变成了产品能力，也是答辩时最硬的证据。

    返回三部分：
      tools         可调用的工具（即"能做什么"）
      blocked       答不了的主题及原因（即"不做什么"）
      basis_meaning 回答口径字段的语义（前端展示"这句话怎么来的"）
    """
    from app.agent import tools as tools_mod

    return {
        "tools": [
            {"name": n,
             "write": s["write"],
             # label 是面向用户的中文说明（见 tools.TOOL_SPECS 的说明）
             "label": s.get("label") or s["description"],
             "description": s["description"]}
            for n, s in tools_mod.TOOL_SPECS.items()
        ],
        # ⚠ `blocked` 保留在接口里（供其它调用方与测试使用），
        #   但**前端能力面板不再展示它** —— 用户要求能力范围只列可做项。
        #   拦截逻辑本身不受影响：guard_rules 仍在调用模型前拦下这三类问题。
        "blocked": guard_rules.known_topics(),
        "basis_meaning": {
            "llm_verified": "卡片来自系统数据，模型解读通过数值核对，语义仍需复核",
            "system_data": "服务端依据会话任务执行的确定性查询",
            "llm_partial_dropped": "模型解读含无法溯源的数字，已丢弃该段；数据卡片不受影响",
            "system_meta": "系统信息类问题，由确定性模板回答（不经过模型）",
            "no_data": "模型未查询任何系统数据即作答，未经数据核对",
            "guard_blocked": "该问题触及本系统答不了的边界，已在调用模型前拦截",
            "system_pending_action": "系统生成的待确认操作说明（未执行写操作）",
            "disabled": "智能体未启用",
        },
    }


@router.post("/ask")
def agent_ask(body: AskRequest, request: Request,
                    user: User = Depends(
                        auth_deps.require_perm(auth.PERM_AGENT_USE)),
                    db: Session = Depends(get_db)):
    """问一句，得到一个基于系统真实数据的回答。

    回答可能附带：
      pending_action  待用户确认的写操作（如建单）—— 不会自动执行
      citations       本次回答用到的数据来源
      verify_failed   被丢弃的、无法溯源的数字（非空说明模型编过数字）
      context         更新后的会话上下文 —— 前端存下，下一轮原样回传

    context返回有限实体指针与名单游标。服务端状态默认一小时有效，
    已展示名单最多50位、累计排除最多500位；不保存无限对话原文。
    """
    ok, reason = llm.is_available()
    if not ok:
        # 明确失败，不降级 —— 见文件顶部说明
        raise HTTPException(
            status_code=503,
            detail=f"智能体当前不可用：{reason}",
        )

    from app.agent import graph as graph_mod

    q = body.question.strip()
    if not q:
        raise HTTPException(status_code=422, detail="问题不能为空")

    ctx, version = _start_session(db, user, body)
    result = {}
    completed = False
    explicit = ctx_mod.explicit_ids(q)
    if len(explicit) == 1:
        # 即使查无工单，也不能让下一句“该工单”回到更早的其他客户。
        ctx["focus_customer_id"] = explicit[0].upper()
        ctx["orders"] = [o for o in ctx["orders"]
                         if o.get("customer_id") == ctx["focus_customer_id"]]
    try:
        op = order_safety.operation(q)
        if op:
            if not auth.has_perm(user.role or "", auth.PERM_ORDER_WRITE):
                raise HTTPException(403, "无工单写入权限")
            order = order_safety.resolve(db, user, q, ctx, cancel=op == "cancel")
            pending = order_safety.proposal(order, delete=op == "delete")
            result = _pending_reply(pending, ctx)
        else:
            result = graph_mod.run(q, session_id=body.session_id,
                                   context=ctx)
        pending = result.get("pending_action")
        if pending:
            action = pending.get("action")
            kind = ("create" if action in ("create_work_order", "create_work_order_batch")
                    else "delete" if action == "delete_work_order"
                    else "cancel" if (pending.get("payload") or {}).get("status") == "cancelled" else "update")
            ids = ([it.get("customer_id") for it in pending.get("items", [])]
                   if action == "create_work_order_batch" else (pending.get("payload") or {}).get("customer_id"))
            error = guard_rules.write_action_error(q, kind, ids)
            if error:
                raise ValueError(error)
        if pending and pending.get("action") == "create_work_order_batch":
            plan = pending
            pending = _prepare_create(db, user,
                [it["customer_id"] for it in pending.get("items", [])],
                (pending.get("items") or [{}])[0].get("assignee", ""))
            pending["failed"] += plan.get("failed") or []
            for key in ("requested_customer_ids", "remaining_customer_ids", "batch_limit"):
                if key in plan:
                    pending[key] = plan[key]
            if not pending["items"] and not pending.get("remaining_customer_ids"):
                raise ValueError("；".join(it["reason"] for it in pending["failed"]))
            result = _pending_reply(pending, ctx)
        if pending and pending.get("action") in (*order_safety.ORDER_ACTIONS, "create_work_order"):
            if not auth.has_perm(user.role or "", auth.PERM_ORDER_WRITE):
                raise HTTPException(403, "无工单写入权限")
            if pending["action"] in order_safety.ORDER_ACTIONS:
                p = pending["payload"]
                if pending["action"] == "delete_work_order" and op != "delete":
                    raise ValueError("永久删除必须明确提出；取消工单只会改为已取消并保留记录。")
                if p.get("status") == "cancelled" and op != "cancel":
                    raise ValueError("请明确指定要取消的客户或工单。")
                order = order_safety.resolve(db, user, q, ctx,
                                             cancel=p.get("status") == "cancelled")
                if order.id != p.get("order_id"):
                    raise ValueError("模型提出的工单与当前目标不一致，已拦截，请指定工单编号重新操作。")
                p["customer_id"] = order.customer_id
            order_safety.issue(db, user, pending)
            result = _pending_reply(pending, ctx)
        completed = True
    except ValueError as e:
        result = {"text": str(e), "answer": {"kind": "text", "headline": str(e),
                  "entities": [], "facts": [], "actions": [], "insights": [], "text": str(e)},
                  "basis": "system_meta", "pending_action": None,
                   "context": ctx, "citations": [], "tool_calls": []}
        completed = True
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("agent: 执行失败")
        raise HTTPException(
            status_code=500,
            detail=f"智能体执行失败：{type(e).__name__}: {e}" if llm.settings.DEBUG
                   else "智能体执行失败，请稍后重试或联系管理员。",
        )
    finally:
        db.rollback()
        session = db.get(order_safety.AgentSession, (user.username, body.session_id))
        if session:
            if completed:
                session.context = json.dumps(result.get("context") or ctx, ensure_ascii=False)
                session.version += 1
                version = session.version
            session.busy_until = 0
            session.expires_at = time.time() + SESSION_TTL_SECONDS
            db.commit()

    return {
        "question": q,
        "session_id": body.session_id,
        "session_version": version,
        **result,
    }


def _start_session(db, user, body):
    model = order_safety.AgentSession
    now = time.time()
    row = db.get(model, (user.username, body.session_id))
    if row is None:
        row = model(username=user.username, session_id=body.session_id, context="{}",
                    version=0, expires_at=now + SESSION_TTL_SECONDS, busy_until=0)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "会话正在初始化，请稍后重试")
    expired = row.expires_at <= now
    if not expired and body.session_version is not None and body.session_version != row.version:
        raise HTTPException(409, "会话已在其他窗口更新，请刷新会话后重试")
    ctx = ctx_mod.normalize(json.loads(row.context)) if not expired else ctx_mod.empty()
    if row.version == 0:
        # 兼容旧前端的实体指针；客户端数值与筛选任务不作为可信事实。
        ctx = ctx_mod.normalize(body.context)
        ctx.pop("list_query", None)
        for c in ctx["customers"]:
            c["prob"], c["balance"] = None, None
    version = row.version
    claimed = db.query(model).filter(model.username == user.username,
        model.session_id == body.session_id, model.version == version,
        model.busy_until <= now).update({"busy_until": now + 180}, synchronize_session=False)
    if claimed != 1:
        db.rollback()
        raise HTTPException(409, "当前会话正在处理上一条请求，请等待完成")
    db.query(model).filter(model.expires_at < now, model.busy_until <= now).delete(synchronize_session=False)
    db.commit()
    return ctx, version


def _pending_reply(pending, context):
    from app.agent.graph import node_verify
    out = node_verify({"pending_action": pending, "tool_results": []})
    return {"answer": out["answer"], "text": out["final"],
            "basis": out["answer_basis"], "pending_action": pending,
            "context": ctx_mod.merge(ctx_mod.normalize(context), ctx_mod.extract([], pending)),
            "citations": [], "tool_calls": [], "verify_failed": []}


@router.get("/session/{session_id}")
def read_session(session_id: str,
                 user: User = Depends(auth_deps.require_perm(auth.PERM_AGENT_USE)),
                 db: Session = Depends(get_db)):
    row = db.get(order_safety.AgentSession, (user.username, session_id))
    if row is None or row.expires_at <= time.time():
        return {"context": ctx_mod.empty(), "session_version": 0, "expired": True,
                "context_ttl_seconds": SESSION_TTL_SECONDS, "expires_at": None}
    return {"context": json.loads(row.context), "session_version": row.version, "expired": False,
            "context_ttl_seconds": SESSION_TTL_SECONDS, "expires_at": row.expires_at}


class RevokeRequest(BaseModel):
    confirmation_tokens: list[str] = Field(default_factory=list, max_length=1000)


def _revoke_tokens(db, user, tokens):
    import hashlib
    hashes = [hashlib.sha256(token.encode()).hexdigest() for token in tokens]
    if hashes:
        db.query(order_safety.PendingAgentAction).filter(
            order_safety.PendingAgentAction.username == user.username,
            order_safety.PendingAgentAction.token_hash.in_(hashes)).update(
                {"consumed": 1}, synchronize_session=False)


@router.post("/retract")
def retract_action(body: RevokeRequest,
                   user: User = Depends(auth_deps.require_perm(auth.PERM_AGENT_USE)),
                   db: Session = Depends(get_db)):
    _revoke_tokens(db, user, body.confirmation_tokens)
    db.commit()
    return {"retracted": True}


@router.post("/session/{session_id}/clear")
def clear_session(session_id: str, body: RevokeRequest,
                  user: User = Depends(auth_deps.require_perm(auth.PERM_AGENT_USE)),
                  db: Session = Depends(get_db)):
    row = db.get(order_safety.AgentSession, (user.username, session_id))
    if row and row.busy_until > time.time():
        raise HTTPException(409, "会话请求尚在处理，完成后再清空")
    if row:
        row.context = "{}"
        row.version += 1
        row.expires_at = 0
    _revoke_tokens(db, user, body.confirmation_tokens)
    db.commit()
    return {"cleared": True}


class PrepareCancelRequest(BaseModel):
    order_id: int = Field(gt=0)
    customer_id: str = Field(pattern=r"^C\d{4,}$")


@router.post("/prepare-order-cancel")
async def prepare_order_cancel(body: PrepareCancelRequest,
                               user: User = Depends(auth_deps.require_perm(auth.PERM_ORDER_WRITE)),
                               db: Session = Depends(get_db)):
    """列表选择的单子也必须先准备并展示快照，再由用户确认。"""
    try:
        order = order_safety.resolve(db, user, f"取消 {body.customer_id} 工单 #{body.order_id}",
                                     None, cancel=True)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return order_safety.issue(db, user, order_safety.proposal(order))


@router.post("/confirm")
async def agent_confirm(payload: dict, request: Request,
                        user: User = Depends(
                            auth_deps.require_perm(auth.PERM_ORDER_WRITE))):
    """确认执行 Agent 提议的写操作。

    ⚠ 与 /ask 分离是刻意的：Agent 只能**提议**，执行必须由这个
      独立端点完成，且由用户显式触发。这样"Agent 不会自作主张写库"
      是**接口结构**上的保证，而不是靠提示词约束模型。

    ⚠ 引入登录后的关键变化：**操作者由服务端从会话取**（`user`），
      不再从 payload 里读、也不再依赖 LLM 抽出的 assignee。
      此前 `session_id` 硬编码为 'ui'，Agent 建的工单**追不到是谁让建的**；
      现在每次写操作都留审计（4A 的 Audit 环节）。

    单条建单/改单/删除都须使用 /ask 返回的十分钟一次性确认凭据。
    凭据占用、业务写入与审计在同一事务内提交；取消是 cancelled 状态而非删除。
    """
    from app.database import SessionLocal
    from app.routers.work_orders import (
        create_work_order, delete_work_order, get_work_order, update_work_order)
    from app.schemas.work_order import WorkOrderCreate, WorkOrderUpdate

    action = payload.get("action")
    if action not in ("create_work_order", "update_work_order", "delete_work_order"):
        raise HTTPException(status_code=422,
                            detail=f"不支持的 action：{action}")

    body = payload.get("payload")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="缺少 payload")

    db = SessionLocal()
    try:
        action, body = order_safety.claim(db, user, payload)
        db.info["agent_transaction"] = True
        if action == "create_work_order":
            # ── 负责人兜底：Agent 抽出的人未必有效 ──────────────
            #
            # ⚠ 实测背景：assignee 原先是 LLM 从用户话里抽的自由文本，
            #   用户没指定时它会**编一个名字**（如"张经理"），
            #   而库里根本没有这个人 —— 派单派给了查无此人。
            #
            #   现在后端建单会校验取值有效性（见 work_orders._is_valid_assignee），
            #   故这里必须兜底：**抽不到有效的人，就派给当前登录人**。
            #   这是合理的默认 —— 谁让 Agent 建的，就默认谁跟进；
            #   页面入口也是这个默认值，两个入口行为一致。
            #
            #   注意：不在这里抛错。用户在对话里说"帮我建个单"却没提负责人
            #   是很自然的表达，不该被拒；把它派给发起人并如实显示即可。
            body = dict(body)
            cand = str(body.get("assignee") or "").strip()
            if not cand:
                body["assignee"] = user.username
            # 复用既有建单逻辑（含负责人校验、409 互斥、渠道覆盖校验、快照补齐），
            # 不另写一套 —— 否则 Agent 建的工单与页面建的会有口径差异
            # ⚠ 传 request 是为了让建单逻辑里的审计能记到真实客户端 IP
            created = await create_work_order(
                WorkOrderCreate(**body), request, user=user, db=db)
            auth_deps.write_audit(
                db, user=user, action="create_work_order",
                target=str(created.get("customer_id") or ""),
                detail={"order_id": created.get("id"),
                        "assignee": created.get("assignee"),
                        "via": "agent"},
                source="agent", ip=auth_deps._client_ip(request),
                message=f"通过智能助手为客户 {created.get('customer_id')} 创建挽留工单")
            db.commit()
            return {"created": True, "action": action, "order": created}

        if action == "update_work_order":
            oid = int(body.get("order_id"))
            fields = {k: v for k, v in body.items()
                      if k in ("status", "assignee", "note") and v is not None}
            if not fields:
                raise HTTPException(status_code=422,
                                    detail="update_work_order 至少需要 status/assignee/note 之一")
            updated = await update_work_order(
                oid, WorkOrderUpdate(**fields), request, user=user, db=db)
            auth_deps.write_audit(
                db, user=user, action="update_work_order",
                target=f"#{oid}",
                detail={"order_id": oid, "fields": sorted(fields.keys()),
                        "customer_id": updated.get("customer_id"), "status": updated.get("status"),
                        "via": "agent"},
                source="agent", ip=auth_deps._client_ip(request),
                message=f"通过智能助手{'取消（保留记录）' if updated.get('status') == 'cancelled' else '修改'}工单 #{oid}")
            db.commit()
            return {"created": False, "action": action, "order": updated}

        # delete_work_order
        oid = int(body.get("order_id"))
        # ⚠ 删前先取快照 —— 删掉之后就无法向用户交代"删的是哪一张"
        before = None
        try:
            detail = await get_work_order(oid, user=user, db=db)
            before = {"customer_id": detail.get("customer_id"),
                      "customer_name": detail.get("customer_name"),
                      "status": detail.get("status")}
        except HTTPException:
            pass
        await delete_work_order(oid, request, user=user, db=db)
        auth_deps.write_audit(
            db, user=user, action="delete_work_order", target=f"#{oid}",
            detail={"order_id": oid, "deleted_order": before, "via": "agent"},
            source="agent", ip=auth_deps._client_ip(request),
            message=f"通过智能助手删除工单 #{oid}")
        db.commit()
        return {"created": False, "action": action, "deleted": True,
                "order_id": oid, "deleted_order": before}

    except HTTPException as e:
        db.rollback()
        # 失败也要留痕 —— 审计要能回答"谁尝试过什么但没成功"
        try:
            auth_deps.write_audit(
                db, user=user, action=action or "unknown",
                target=str((payload.get("payload") or {}).get("customer_id")
                           or (payload.get("payload") or {}).get("order_id") or ""),
                detail={"via": "agent"}, source="agent", success=False,
                message=str(e.detail)[:200],
                ip=auth_deps._client_ip(request))
            db.commit()
        except Exception:
            db.rollback()
        raise
    except (TypeError, ValueError) as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=f"参数错误：{e}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500,
                            detail=f"操作失败：{type(e).__name__}: {e}")
    finally:
        db.close()


class PrepareCreateRequest(BaseModel):
    customer_ids: list[str] = Field(min_length=1, max_length=20)
    assignee: str = Field(default="", max_length=64)


def _prepare_create(db, user, ids, assignee=""):
    if not auth.has_perm(user.role or "", auth.PERM_ORDER_WRITE):
        raise HTTPException(403, "无工单写入权限")
    from app.services.customer_service import get_retention_customer
    from app.services import note_service
    items, failed = [], []
    for cid in dict.fromkeys(c.strip().upper() for c in ids):
        try:
            d = get_retention_customer(db, cid)
            note = note_service.build_reason(prob=d["probability"], balance=d["balance"],
                risk_level=d["risk_level"], value_tier=d["value_tier"],
                risk_factors=d.get("risk_factors", []), expected_value=d["expected_value"],
                action=d.get("action"), channel=d.get("channel"))["note"]
            proposal = {"action": "create_work_order", "payload": {
                "customer_id": cid, "assignee": assignee, "note": note}}
            order_safety.issue(db, user, proposal)
            items.append({**proposal["payload"], "confirmation_token": proposal["confirmation_token"]})
        except HTTPException as e:
            db.rollback()
            failed.append({"customer_id": cid, "reason": str(e.detail)})
    return {"action": "create_work_order_batch", "items": items, "failed": failed,
            "summary": f"为 {len(items)} 位客户准备挽留工单", "requires_confirmation": True}


@router.post("/prepare-create")
def prepare_create(body: PrepareCreateRequest,
                   user: User = Depends(auth_deps.require_perm(auth.PERM_ORDER_WRITE)),
                   db: Session = Depends(get_db)):
    return _prepare_create(db, user, body.customer_ids, body.assignee)


class BatchConfirmRequest(BaseModel):
    proposals: list[dict] = Field(min_length=1, max_length=20)


@router.post("/confirm-batch")
async def agent_confirm_batch(body: BatchConfirmRequest, request: Request,
                             user: User = Depends(auth_deps.require_perm(auth.PERM_ORDER_WRITE))):
    """逐项消费已审阅的一次性凭据；失败单独汇报，不重建未批准的快照。"""
    created, failed = [], []
    for proposal in body.proposals:
        if proposal.get("action") != "create_work_order":
            raise HTTPException(422, "批量建单只接受 create_work_order 提议")
    for proposal in body.proposals:
        cid = (proposal.get("payload") or {}).get("customer_id")
        try:
            result = await agent_confirm(proposal, request, user=user)
            created.append(result["order"])
        except HTTPException as e:
            failed.append({"customer_id": cid, "reason": str(e.detail)})
    return {"requested": len(body.proposals), "succeeded": len(created),
            "failed_count": len(failed), "skipped_count": 0,
            "created": created, "failed": failed, "skipped": []}
