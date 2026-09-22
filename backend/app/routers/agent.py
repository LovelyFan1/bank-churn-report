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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import guard_rules, llm
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
                            description="会话标识（MVP 未持久化，仅回显）")
    # 会话上下文（短期记忆）——由**前端回传**，见下。
    #
    # ⚠ 为什么是前端回传而不是服务端保存：
    #   生产用 `uvicorn --workers 4`，若放进程内存则 4 个 worker 各自为政，
    #   追问会随机命中"没有记忆"的那个，表现为"有时记得有时不记得"——
    #   最难排查的一类故障。回传方式让后端保持**无状态**，多 worker 天然一致。
    #
    # ⚠ 它是**不可信输入**（用户能改 localStorage），故 graph 侧会逐字段
    #   校验（见 context.normalize），且编号还要过输出侧白名单。
    #
    # ⚠ 类型刻意用 Any 而不是 dict：若声明为 dict，Pydantic 会在进 graph
    #   之前就抛 422，**整轮对话直接挂掉** —— 而上下文坏了不该让用户
    #   问不了问题。用 Any 把校验权交给 context.normalize。
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
            "llm_verified": "模型作答，且其中每个数字都已通过溯源校验",
            "llm_partial_dropped": "模型解读含无法溯源的数字，已丢弃该段；数据卡片不受影响",
            "system_meta": "系统信息类问题，由确定性模板回答（不经过模型）",
            "no_data": "模型未查询任何系统数据即作答，未经数据核对",
            "guard_blocked": "该问题触及本系统答不了的边界，已在调用模型前拦截",
            "system_pending_action": "系统生成的待确认操作说明（未执行写操作）",
            "disabled": "智能体未启用",
        },
    }


@router.post("/ask")
async def agent_ask(body: AskRequest, request: Request,
                    user: User = Depends(
                        auth_deps.require_perm(auth.PERM_AGENT_USE)),
                    db: Session = Depends(get_db)):
    """问一句，得到一个基于系统真实数据的回答。

    回答可能附带：
      pending_action  待用户确认的写操作（如建单）—— 不会自动执行
      citations       本次回答用到的数据来源
      verify_failed   被丢弃的、无法溯源的数字（非空说明模型编过数字）
      context         更新后的会话上下文 —— 前端存下，下一轮原样回传

    ⚠ `context` 是**短期记忆的全部载体**：只存编号等指针，不存对话原文、
      也不存工具原始结果。故追问次数不限、每轮成本恒定（约 100 token），
      且指针不会"记错"。详见 app/agent/context.py。
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

    try:
        result = graph_mod.run(q, session_id=body.session_id,
                               context=body.context)
    except Exception as e:
        logger.exception("agent: 执行失败")
        raise HTTPException(
            status_code=500,
            detail=f"智能体执行失败：{type(e).__name__}: {e}",
        )

    return {
        "question": q,
        "session_id": body.session_id,
        **result,
    }


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

    当前支持 action=create_work_order。
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
                        "via": "agent"},
                source="agent", ip=auth_deps._client_ip(request),
                message=f"通过智能助手修改工单 #{oid}")
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
        raise HTTPException(status_code=422, detail=f"参数错误：{e}")
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"操作失败：{type(e).__name__}: {e}")
    finally:
        db.close()


class BatchConfirmRequest(BaseModel):
    customer_ids: list[str] = Field(min_length=1, max_length=20,
                                    description="要建单的客户编号列表")
    assignee: str = Field(default="", max_length=64,
                          description="统一负责人，留空则不指派")


@router.post("/confirm-batch")
async def agent_confirm_batch(body: BatchConfirmRequest, request: Request,
                              user: User = Depends(
                                  auth_deps.require_perm(auth.PERM_ORDER_WRITE))):
    """批量确认建单 —— 方案 A（弹确认面板后一次性提交）。

    ⚠ 为什么需要独立端点而不是前端循环调 /confirm：
      1) **部分失败要能被结构化汇报**。实测（diag112）三人里可能有一人
         已有进行中工单 → 409。前端若自己循环，就得自己拼错误信息，
         容易出现"两条成功一条失败但提示含糊"。
      2) **互斥检查要在服务端统一做**。这里先查一遍
         /work-orders/active-customers，把已有工单的人**提前标出**，
         而不是等 409 再回头解释。
      3) 失败**不中断**后续 —— 一条失败不应让其余全部回滚，
         否则用户要重来一遍。

    返回：
      ok            成功建单的客户编号
      failed        失败列表 [{customer_id, reason}]
      skipped       因已有进行中工单而跳过的客户编号
      created       新建工单的摘要（id / customer_id / assignee）
    """
    from app.database import SessionLocal
    from app.routers.work_orders import create_work_order
    from app.schemas.work_order import WorkOrderCreate
    from app.services.customer_service import get_customer_detail

    ids = [c.strip() for c in body.customer_ids if c and c.strip()]
    if not ids:
        raise HTTPException(status_code=422, detail="customer_ids 不能为空")

    # 去重但保序 —— 用户可能重复勾选
    seen = set()
    uniq = [c for c in ids if not (c in seen or seen.add(c))]

    db = SessionLocal()
    ok_list: list[dict] = []
    failed: list[dict] = []
    skipped: list[str] = []

    try:
        # 预先查一次"已有进行中工单"的客户，提前归类
        from app.models.work_order import WorkOrder
        active = {
            r[0] for r in db.query(WorkOrder.customer_id)
            .filter(WorkOrder.status.in_(["pending", "in_progress"])).all()
        }

        for cid in uniq:
            if cid in active:
                skipped.append(cid)
                continue

            detail = get_customer_detail(db, cid)
            if detail is None:
                failed.append({"customer_id": cid, "reason": "客户不存在或模型未训练"})
                continue

            # 组装建单体 —— 与 propose_create_work_order 工具同源字段，
            # 保证 Agent 建的工单与页面建的完全一致
            suggested = ""
            try:
                from app.services import note_service
                suggested = note_service.build_reason(
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
                pass   # 理由生成失败不阻断建单，note 留空即可

            payload = {
                "customer_id": detail.get("customer_id"),
                "customer_name": detail.get("surname"),
                "geography": detail.get("geography"),
                "risk_level": detail.get("risk_level"),
                "probability": detail.get("probability"),
                "balance": detail.get("balance"),
                "risk_factors": detail.get("risk_factors") or [],
                "strategy": detail.get("action") or detail.get("strategy") or "",
                # ⚠ 与单建路径同一兜底：用户没指定负责人时默认派给**发起人**。
                #   不这样做的后果（实测逻辑推演）：assignee="" 会被
                #   create_work_order 的必填校验拒掉，整批全部失败 ——
                #   而用户在对话里只说"帮这几个人建单"是很自然的表达。
                "assignee": (body.assignee or "").strip() or user.username,
                "note": suggested,
                "channel": detail.get("channel"),
                "value_tier_snapshot": detail.get("value_tier"),
                "expected_value_snapshot": detail.get("expected_value"),
            }
            try:
                # ⚠ 必须与 create_work_order 的签名一致（body, request, user, db）。
                #   漏掉 request/user 时 Python **导入阶段不报错**，
                #   只在真正调用时抛 TypeError —— 表现为 Agent 批量建单整条崩掉，
                #   而静态检查与模块导入都看不出问题（本缺陷即由签名一致性
                #   检查脚本发现，非人工阅读）。
                created = await create_work_order(
                    WorkOrderCreate(**payload), request, user=user, db=db)
                ok_list.append({
                    "id": created.get("id"),
                    "customer_id": created.get("customer_id"),
                    "customer_name": created.get("customer_name"),
                    "assignee": created.get("assignee"),
                })
                # 立刻加入 active，防止同一批里重复建（理论上已去重，双保险）
                active.add(cid)
            except HTTPException as e:
                failed.append({"customer_id": cid, "reason": str(e.detail)})
            except Exception as e:
                failed.append({"customer_id": cid,
                               "reason": f"{type(e).__name__}: {e}"})

        return {
            "requested": len(uniq),
            "succeeded": len(ok_list),
            "failed_count": len(failed),
            "skipped_count": len(skipped),
            "created": ok_list,
            "failed": failed,
            "skipped": skipped,
        }
    finally:
        # ── 批量操作审计（无论成败都留痕）────────────────────
        # 放在 finally 里：即使中途抛异常，"谁尝试批量建过单"也必须可查。
        try:
            auth_deps.write_audit(
                db, user=user, action="create_work_order_batch",
                target=f"{len(ok_list)}/{len(uniq)}",
                detail={"requested": uniq, "succeeded": [o["customer_id"] for o in ok_list],
                        "failed": failed, "skipped": skipped, "via": "agent"},
                source="agent", success=not failed and bool(ok_list),
                message=(f"通过智能助手批量建单：成功 {len(ok_list)}、"
                         f"失败 {len(failed)}、跳过 {len(skipped)}"),
                ip=auth_deps._client_ip(request))
            db.commit()
        except Exception:
            db.rollback()
        db.close()
