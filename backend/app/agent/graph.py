"""LangGraph 状态机 —— Agent 的控制流。

**为什么用图而不是 while 循环**

朴素实现是「调 LLM → 看有没有 tool_calls → 有就执行 → 再调」的循环。
那种写法在本项目行不通，因为这里有**两条必须能中止的路径**：

  1. Guard 命中 → 必须立刻结束，**不得**调用 LLM（否则它会去编）
  2. Verify 不通过 → 必须**回退到模板**，而不是把编造的回答发给用户

在 while 循环里，这两条只能靠到处 if/break 实现，无法观测、无法测试、
也无法单步调试。图把每个判断点变成**显式节点与条件边**。

**节点职责（严格单一）**

    guard      确定性拒答，不碰 LLM
    agent      LLM 决策：调工具 or 直接回答（ReAct 风格的唯一 LLM 节点）
    tools      执行工具（只读直接执行；写操作转 pending_action）
    verify     数字校验，不过则回退模板
    fallback   生成确定性模板回答（不经过 LLM）

**MVP 的边界（刻意不做）**

    · 无 RAG 检索节点 —— 语料检索二期加
    · 无 interrupt() 图内中断 —— 写操作用"返回待确认 + 前端按钮"实现
    · 无 checkpointer —— 会话不持久化
    · 无流式

这些将来都是**加节点**，不是重构图 —— 这是先用图的意义。
"""

import json
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from app.agent import (answer_builder, context as ctx_mod, guard_rules, llm,
                       tools, verify as verify_mod)
from app.agent.state import AgentState, initial_state
from app.config import settings

logger = logging.getLogger(__name__)

# ── 系统提示词（只给"决策"节点用，写手节点用另一段）──────

_DECISION_SYSTEM = """你是银行客户流失预警系统的分析助手。

你可以调用工具查询系统的真实数据。请遵守：

1. **先查再答**。任何涉及客户、阈值、金额、人数的问题，都必须先调用工具。
   你没有记忆里的数字，只有工具返回的数字。
2. **不要自己算派生量**。不要计算总计、平均、占比 —— 如果工具没返回，
   就不要给。宁可说"这需要另查"，也不要估算。
3. **只读工具可直接调用**。当用户要求"建单""派单"这类写操作时，
   调用 propose_create_work_order —— 它只会生成待确认动作，不会真的执行。
   然后告诉用户「已生成待确认的建单请求」，**绝不能说"已经建好了"**。
4. **样本不全就说不全**。工具返回里若有 is_complete=false 或 note 提示
   只返回了前 N 条，你必须如实说明，不得把部分名单说成全部。

用中文回答。回答要简洁、具体，直接给数字和结论。
"""

_WRITER_SYSTEM = """你是银行客户流失预警系统的分析助手。

下面会给你**工具刚刚返回的真实数据**。请输出两样东西：

1. `headline`：一句话结论，不超过 25 字，直接给答案。
   例：「挽回价值最高的 3 位客户」「当前决策阈值 0.6」。

2. `insights`：0~3 条短句解读，**每条不超过 40 字**。
   只写你在数据里**真实看到的**对比或规律。
   没有值得说的就留空数组 —— 不要为凑数而写。

⚠ 严格按 JSON 输出，不要 Markdown 代码块，不要任何解释文字：
{"headline": "...", "insights": ["...", "..."]}

铁律（违反即作废）：
1. 出现的**每一个数字都必须来自给定数据**，不得推算、估算、编造。
2. 不要做加减乘除。要表达关系就说「高于」「低于」，不要给出自己算的数。
3. **不要输出表格、不要输出 Markdown 标记**（# * | ` 等）——
   客户卡片、指标明细、按钮都由系统用结构化组件渲染。
4. **不要在 insights 里重复卡片上已经显示的数值**。卡片已经用
   「22.0 万」「97.6%」这样的格式展示了每个客户的期望挽回、概率、余额，
   你再写一遍 `220080.25` 既冗余又难读。要写就写**对比与原因**，
   需要指代某个客户时用姓名，不要用数字。
   反例（差）：「Bentley 期望挽回 220080.25 居首」
   正例（好）：「Bentley 期望挽回居首，但已有在途工单」
5. **枚举值必须译成中文**：CRITICAL→极高、HIGH→高危、MEDIUM→中等、
   LOW→低风险；价值层 HIGH→高价值、LOW→中低价值、ZERO→零余额。
   不要在中文句子里夹带 CRITICAL/HIGH 这类英文。
   ⚠ 价值层的 LOW 是「中低价值」不是「低价值」—— 该档实测平均余额 8.2 万，
     叫「低价值」会让用户误以为是几千块的小客户。
6. 语气专业简洁。

用中文回答。
"""


# ══════════════════════════════════════════════════════════
# 节点实现
# ══════════════════════════════════════════════════════════

def node_guard(state: AgentState) -> dict:
    """① 拒答拦截 —— 确定性，不调 LLM。

    这是本图的第一道也是最硬的一道闸门。命中即结束，LLM 根本没机会开口。
    """
    q = state.get("question", "")
    hit = guard_rules.check(q)
    if hit is None:
        return {"guard_block": None, "guard_topic": None}

    logger.info("agent: guard 拦截 topic=%s q=%s", hit.key, q[:40])
    msg = hit.to_message()
    return {
        "guard_block": msg,
        "guard_topic": hit.key,
        "final": msg,
        "answer": {
            "kind": "blocked",
            "headline": "这个问题我答不了",
            "warning": {"level": "guard", "text": hit.reason, "hint": hit.instead},
            "entities": [], "facts": [], "actions": [], "insights": [],
            "text": msg,
            "guard_topic": hit.key,
        },
        "answer_basis": "guard_blocked",
        "citations": [f"guard://{hit.key}"],
    }


def node_meta(state: AgentState) -> dict:
    """①b 元问题 —— 确定性回答（问系统自身），不调 LLM。

    ⚠ 为什么单独一个节点、且放在 Guard 之后、Agent 之前：
      实测（diag126）问「可调用工具？」模型答「当前无可用工具」，
      而系统实际有 9 个工具 —— 错误答案配 `llm_verified`。
      根因是模型看不见自己的工具清单，只能凭"没有数据"来搪塞。

      这类问题的答案**有唯一事实来源**（工具注册表 / 拒答规则 / 依赖清单），
      因此拼出来即可，零幻觉可能，也省一次 LLM 调用。
      放在 Agent 之前是为了**根本不给模型瞎答的机会**。

    不是元问题时返回空 dict，图继续走 Agent。
    """
    from app.agent import meta

    ans = meta.answer(state.get("question", ""))
    if ans is None:
        return {}
    logger.info("agent: 元问题由确定性模板回答 kind=%s", ans.get("meta_kind"))
    return {
        "answer": ans,
        "final": ans.get("text") or ans.get("headline") or "",
        # 口径：系统模板生成，与 LLM 无关
        "answer_basis": "system_meta",
        "citations": [f"meta://{ans.get('meta_kind')}"],
    }


def _context_block(state: AgentState) -> str:
    """渲染要注入 LLM 的会话上下文块（无则空串）。

    ⚠ 注入在 **HumanMessage 里**（用户问句前部），**不是** SystemMessage。
      原因：DeepSeek 的上下文缓存按前缀命中给折扣，而 system prompt 与
      工具定义是天然静态的部分。把每轮都变的上下文插在最前面会让前缀
      全变 → 缓存全废，比不缓存更亏。详见 agent/context.py 顶部说明。

    ⚠ 省 token 规则：若用户在问句里**显式写出了编号**，且该编号不在
      上下文名单里，说明这是全新话题 —— 不必注入旧名单（约 100 token），
      也避免模型把新客户与旧名单混在一起。
      反过来说，"显式编号 + 命中名单"（如「这三个人里 C071081 呢」）
      仍要注入，那正是需要指代消解的场景。

    成本不对称是这条规则的理由：误注入只多花约 100 token（无害），
    漏注入则直接答不了。故宁可偏向注入。
    """
    ctx = state.get("context_in") or {}
    if not ctx:
        return ""
    known = {str(c.get("id") or "").upper()
             for c in ctx.get("customers") or []}
    explicit = ctx_mod.explicit_ids(state.get("question") or "")
    if explicit and not any(e.upper() in known for e in explicit):
        return ""
    return ctx_mod.render(ctx)


def node_agent(state: AgentState) -> dict:
    """② LLM 决策节点 —— 决定调工具还是直接答。

    这是图里**唯一**允许 LLM 决定控制流的地方。

    ⚠ 为什么不把历史 AIMessage/ToolMessage 原样回灌（踩过坑）：
      OpenAI 协议要求每个 tool_call 都必须有配对的 tool_call_id 的
      ToolMessage，否则报 400。而 `add_messages` 只追加、不会保留
      "配对"关系，回灌后极易出现"有 tool_call 无对应结果"的畸形序列。
      故此处每轮**重建一份干净的消息列表**，把已得的工具结果作为
      普通文本附在 HumanMessage 里 —— 简单且不会违反协议。

    ⚠ 会话上下文也走同一条路：**只注入一份指针清单**（编号 + 极简标签），
      不注入历史原文、不注入工具原始结果。见 agent/context.py。
    """
    model = llm.router_llm().bind_tools(_tool_schemas())

    msgs: list = [SystemMessage(content=_DECISION_SYSTEM)]
    block = _context_block(state)
    if block:
        # 上下文在前、问句在后 —— 让模型先看到"有哪些对象"，再读问题
        msgs.append(HumanMessage(content=f"{block}\n\n{state['question']}"))
    else:
        msgs.append(HumanMessage(content=state["question"]))

    made = state.get("tool_calls_made", [])
    if made:
        # 把已执行的结果摘要回灌，让模型知道"已经拿到什么、够不够"
        digest = []
        for rec in made:
            body = json.dumps(rec.get("result"), ensure_ascii=False, default=str)
            if len(body) > 3000:
                body = body[:3000] + "…（截断）"
            digest.append(
                f"- {rec['name']}({json.dumps(rec.get('args') or {}, ensure_ascii=False)})"
                f" → {'成功' if rec.get('ok') else '失败'}\n  {body}"
            )
        msgs.append(HumanMessage(content=(
            "你此前已调用过以下工具，结果如下：\n" + "\n".join(digest) +
            "\n\n如果这些数据已足够回答用户，请直接给出最终回答（不要再调工具）；"
            "如仍缺数据，可继续调用其它工具。"
        )))

    # ── 强制写意图走 propose 工具（确定性，不靠提示词）─────────
    #
    # ⚠ 实测缺陷（diag108）：问「帮我给 C034525 建个挽留工单」时，
    #   LLM 先调 get_customer_risk，然后在**回答正文里自己写了一段
    #   "## 工单信息（待确认）"** —— 用文字模拟了待确认动作。
    #   数据库确实没被写（安全），但前端拿不到 pending_action，
    #   功能完全失效，而回答读起来像"已经准备好了"。
    #
    #   修法与 Guard 同理：**不要求模型自觉，而是用工具层强制**。
    #   bind_tools 的 tool_choice 可以指定必须调用某个工具，
    #   这样"建单必须经过 propose 流程"就是协议保证，不是提示词祈祷。
    write_intent, cids, assignee = guard_rules.detect_write_intent_multi(
        state["question"])

    # ⚠ 指代解析（"这三位"）：问句里没有编号时，从**会话上下文**取对象。
    #
    #   实测缺陷（用户报告）：用户看到列表后说「帮我把这三位建立工单」，
    #   问句里一个编号都没有（编号在上一条回答的卡片里）。
    #   旧实现因此彻底不识别写意图 —— 不强制提议工具、前端拿不到确认面板，
    #   用户只看到一条"未指定负责人"的单条提议。
    #
    #   修法：上下文里已有那批客户（context.py 在每轮回答后写入），
    #   直接取来作为目标。**只取上下文的 customers**，不看 orders ——
    #   建单的对象只能是客户。
    #
    #   ⚠ 必须限定在上下文集内（而不是全库搜索），否则"这三位"可能
    #     被解析成任意客户 —— 那是越权，也会让用户莫名其妙。
    if write_intent and not cids:
        ctx_customers = (state.get("context_in") or {}).get("customers") or []
        for c in ctx_customers:
            cid = c.get("id")
            if cid:
                cids.append(str(cid).upper())
        if cids:
            logger.info("agent: 写意图含指代，从上下文解析出 %d 位客户：%s",
                        len(cids), cids)

    write_tools_done = {r["name"] for r in made}
    # ── 强制提议：按目标数量选工具 ──────────────────────────
    #
    # ⚠ 一位用 propose_create_work_order，多位用 batch 版本。
    #   旧实现只有单数工具 + 只抓第一个 cid，导致用户说"这三位"时
    #   只提议一位（实测缺陷）。现在按 len(cids) 分流。
    _PROPOSE_SINGLE = "propose_create_work_order"
    _PROPOSE_BATCH = "propose_create_work_orders_batch"
    propose_name = _PROPOSE_BATCH if len(cids) > 1 else _PROPOSE_SINGLE

    if (write_intent and cids and propose_name not in write_tools_done):
        logger.info("agent: 识别到写意图，强制调用 %s cids=%s",
                    propose_name, cids)
        # 规则层已抽出负责人时，用提示词明确告知 —— 强制 tool_choice 时
        # 模型没有机会做复杂推理，必须把参数直接喂给它
        hint = ""
        if assignee:
            hint = (f"注意：用户要求的负责人是「{assignee}」。"
                    f"调用 {propose_name} 时 assignee 参数必须填「{assignee}」。")
        # 多位时把编号**逐个列出**，避免模型自行猜测或漏填
        if len(cids) > 1:
            hint += (f"\n用户指定的客户编号是：{', '.join(cids)}。"
                     f"必须全部放进 customer_ids 参数，一个都不能少。")
        if hint:
            msgs.append(HumanMessage(content=hint))
        model = llm.router_llm().bind_tools(
            _tool_schemas(),
            tool_choice={"type": "function",
                         "function": {"name": propose_name}},
        )
    elif state.get("force_write_tool"):
        # ── 强制调用某个写工具（见 route_after_tools 的说明）────────
        # 场景：用户说"取消工单"，模型先调 list_work_orders 拿到了 order_id，
        # 但**没有**调用 propose_delete_work_order 就要作答。
        # 此时强制它调用正确的提议工具 —— 否则用户看不到确认按钮，
        # 而回答可能读起来像"已经取消了"。
        target = state.get("force_write_tool")
        logger.info("agent: 强制调用写工具 %s", target)
        msgs.append(HumanMessage(content=(
            f"⚠ 你已经查到了需要操作的对象，但还没有调用 {target}。"
            f"请立即调用 {target}，用查到的编号作为参数 —— "
            f"否则用户看不到确认按钮。不要在回答里声称已执行。"
        )))
        model = llm.router_llm().bind_tools(
            _tool_schemas(),
            tool_choice={"type": "function", "function": {"name": target}},
        )
    elif state.get("force_tool"):
        # ── 强制查数据（见 route_after_agent 的说明）──────────
        # 模型上一轮没调任何工具就要作答，而问题需要真实数据。
        # 此时用 tool_choice="required" 强制它必须调一个工具。
        logger.info("agent: 强制模型调用工具（上一轮未调）")
        msgs.append(HumanMessage(content=(
            "⚠ 你刚才没有调用任何工具就要作答。但这个问题**必须**查系统数据"
            "才能回答 —— 你没有关于本系统的记忆，凭印象作答会给出错误答案。"
            "请立即调用合适的工具查询，然后基于返回值回答。"
        )))
        model = llm.router_llm().bind_tools(_tool_schemas(),
                                            tool_choice="required")

    resp = model.invoke(msgs)
    return {"messages": [resp]}


def node_force_tools(state: AgentState) -> dict:
    """强制查数据的中转节点 —— 只置标志，真正的强制在 node_agent 里。

    ⚠ 为什么要单独一个节点而不是直接在条件边里改 state：
      LangGraph 的条件边**只能返回下一跳的名字**，不能修改 state。
      故需要一个真正的节点来写 `force_tool` 标志。

    ⚠ 为什么不会死循环：本节点只走一次 —— route_after_agent 里
      用 `not state.get("force_tool")` 做判据，置位后即便模型仍不调工具，
      也会走 synthesize 而不是再次回到这里。
    """
    return {"force_tool": True}


def node_force_write(state: AgentState) -> dict:
    """强制调用写工具的中转节点（取消/改单场景）。

    决定用哪个写工具：
      pending / in_progress 工单 + 取消意图 → propose_delete_work_order

    ⚠ 也带防重入：置 `force_write_tool` 后，route_after_tools 里
      用 `state.get("force_write_tool")` 判据避免再次进入。
    """
    return {"force_write_tool": "propose_delete_work_order"}


# 哪些工具的参数是"客户编号"，需要走输出侧白名单
_CID_TOOLS = {"get_customer_risk", "propose_create_work_order",
              "propose_create_work_orders_batch"}
# 哪些工具的参数是"工单编号"
_OID_TOOLS = {"propose_update_work_order", "propose_delete_work_order"}


def _scope_error(state: AgentState, name: str, args: dict,
                 results_so_far: list[dict]) -> str | None:
    """输出侧边界校验 —— 编号必须可溯源，否则拒绝执行。

    ⚠ 这是整个会话上下文设计的**关键防线**，也是"通配"能成立的前提：

      输入侧枚举说法（第一个/他/刚才那个…）永远有漏，因为语言是无限的。
      但**输出是有限的**（数据库实体）。所以不在输入侧猜用户在指谁，
      而是让 LLM 自由理解，只校验它最后取用的编号是否在已知名单内。

      这样「他」「那个」「这客户」等任意说法都能被理解（通配），
      而幻觉出来的编号一律进不了工具层（边界内）。

    ⚠ 为什么用"累积 results"而不是只看 state：
      模型可能在同一轮里先 list_customers 再 get_customer_risk，
      此时列表结果还在本轮累积中、尚未写回 state。只看 state 会
      把正常路径误拦。故二者合并。

    ⚠ 名单为空时的两种情形必须都拦：
      · 用户没写编号、上下文也没有 → 模型只能凭空编，必拦
      · 用户写了编号 → 会进 allowed（见 explicit_ids），不会走到这里
    """
    ctx = state.get("context_in") or {}
    question = state.get("question") or ""
    merged = list(state.get("tool_results") or []) + list(results_so_far or [])

    if name in _CID_TOOLS:
        # ⚠ 两种参数名都要处理：单数工具用 customer_id，
        #   批量工具用 customer_ids（列表）。
        #   只判单数会让批量工具**完全绕过白名单校验** ——
        #   模型（或被注入的提示）可以借此给任意客户建单，是越权漏洞。
        raw = args.get("customer_ids")
        if raw is None:
            raw = args.get("customer_id")
        if raw is None or raw == "" or raw == []:
            return None            # 缺参数由工具自身报错，不在此处代判

        cand = raw if isinstance(raw, list) else [raw]
        allowed = ctx_mod.allowed_customer_ids(ctx, question, merged)
        bad = [str(c).upper() for c in cand
               if c and str(c).upper() not in allowed]
        if bad:
            logger.warning("agent: 编号越界拦截 %s cid(s)=%s", name, bad)
            return ctx_mod.out_of_scope_msg(bad[0], allowed)

    elif name in _OID_TOOLS:
        oid = args.get("order_id")
        if oid is None:
            return None
        try:
            oid = int(oid)
        except (TypeError, ValueError):
            return None            # 类型错误交给工具报错，信息更准确
        allowed_o = ctx_mod.allowed_order_ids(ctx, merged)
        if oid not in allowed_o:
            logger.warning("agent: 工单号越界拦截 %s oid=%s", name, oid)
            return ctx_mod.out_of_scope_order_msg(oid, allowed_o)

    return None


def node_tools(state: AgentState) -> dict:
    """③ 执行工具。

    只读工具立即执行；写工具**不执行**，转为 pending_action 交给前端确认。

    ⚠ 去重（实测缺陷 diag126）：模型会**在同一个 AIMessage 里重复发起
      相同的工具调用**。实测问「你有哪些工具」时，3 个工具各被调了 **4 遍**
      （共 12 次调用），延迟与 token 都翻了几倍。
      这是模型行为，不是我们代码的 bug —— 但**执行侧应该挡住**：
      同一轮内 (工具名, 参数) 完全相同的调用只执行一次，后续直接复用结果。
      跨轮不去重：模型可能确实需要重查（例如先查列表再查某一项）。

    ⚠ 越界拦截（会话上下文配套）：带编号的工具在执行前先过 `_scope_error`，
      编号无法溯源则不执行、把原因回给模型让它重决策（见该函数说明）。
    """
    last = None
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            last = m
            break
    if last is None:
        return {}

    made = list(state.get("tool_calls_made", []))
    results = list(state.get("tool_results", []))
    pending = None

    # 本轮内的去重表：key = (工具名, 参数 JSON) → 已执行的结果。
    # ⚠ 作用域仅限**本轮**（本函数一次调用）—— 跨轮不去重，
    #    因为模型可能确实需要重查（先查列表、再查其中某一项）。
    seen_this_round: dict[str, dict] = {}

    for tc in last.tool_calls:
        name = tc["name"]
        args = tc.get("args") or {}
        dedup_key = f"{name}::{json.dumps(args, sort_keys=True, ensure_ascii=False)}"

        if dedup_key in seen_this_round:
            cached = seen_this_round[dedup_key]
            logger.info("agent: 跳过重复工具调用 %s（本轮已执行）", name)
            continue

        # ── 输出侧边界：编号必须可溯源 ────────────────────────
        scope_err = _scope_error(state, name, args, results)
        if scope_err:
            r = {"ok": False, "error": scope_err}
            seen_this_round[dedup_key] = r
            made.append({"name": name, "args": args, "ok": False,
                         "result": None, "error": scope_err})
            results.append({"tool": name, "args": args, "ok": False,
                            "data": {"error": scope_err}})
            continue

        if name in tools.WRITE_TOOL_NAMES:
            # 写操作：生成待确认动作，**不落库**
            r = tools.call_tool(name, args)
            seen_this_round[dedup_key] = r
            made.append({"name": name, "args": args, "ok": r.get("ok", False),
                         "result": r.get("result"), "error": r.get("error")})
            if r.get("ok"):
                pending = r["result"]
            logger.info("agent: 写操作转为待确认 name=%s args=%s", name, args)
            continue

        r = tools.call_tool(name, args)
        seen_this_round[dedup_key] = r
        made.append({"name": name, "args": args, "ok": r.get("ok", False),
                     "result": r.get("result"), "error": r.get("error")})
        results.append({
            "tool": name,
            "args": args,
            "ok": r.get("ok", False),
            "data": r.get("result") if r.get("ok") else {"error": r.get("error")},
        })
        logger.info("agent: 工具 %s ok=%s", name, r.get("ok"))

    return {
        "tool_calls_made": made,
        "tool_results": results,
        "rounds": state.get("rounds", 0) + 1,
        "pending_action": pending,
    }


def _last_decision_text(state: AgentState) -> str:
    """取决策节点**最后一条且没有调用工具**的回答正文。

    用于「模型没查到数据、但它的话本身就是答案」的场景（寒暄、身份、
    常识），见 node_synthesis 里的说明。

    ⚠ 两个必须守住的边界，否则会取错内容：

      1. **只认最后一条**：若走过 force_tools（先答后被强制补查），
         消息里会有两条 AIMessage。必须取**最新**那条 —— 取旧的会把
         已经被强制修正过的回答又拿回来用。
      2. **跳过带 tool_calls 的**：带工具调用的 AIMessage 的 content
         通常是空的或半句话（"让我查一下…"），把它当答案会展示出
         "让我查一下"这种残缺文本。

    ⚠ 注意这里是**倒序找第一条符合条件的**，而不是"看最后一条是否为
      AIMessage"。因为消息列表里最后一条可能已经是 synthesize 阶段追加的
      内容，直接取末条会取错类型。
    """
    for m in reversed(state.get("messages", [])):
        if not isinstance(m, AIMessage):
            continue
        if getattr(m, "tool_calls", None):
            # 带工具调用的不算答案；且它更新，故更早的无工具调用回答
            # 已经被它取代 —— 直接返回空，不继续往前找
            return ""
        text = (m.content or "").strip()
        if text:
            return _cn_enums(text)
        # content 为空且无工具调用：继续往前找（防御性，正常不出现）
    return ""


def node_synthesis(state: AgentState) -> dict:
    """④ 生成回答 —— LLM 只负责措辞，数字已由工具确定。"""
    pending = state.get("pending_action")

    # ── 确定性兜底：写意图必须产出待确认动作 ──────────────────
    #
    # ⚠ 为什么还要这一层（实测缺陷 diag108）：即使加了 tool_choice 强制，
    #   仍不能假设 LLM 一定照做（不同模型/版本对 tool_choice 的支持程度
    #   有差异，且参数可能被改写）。而"用户要求建单、系统却什么都没给"
    #   是**功能性失败**，必须由代码保证，不能靠协议支持度。
    #
    #   故此处直接再判一次写意图：若识别到写意图 + 有客户编号，
    #   却仍没有 pending_action，就**绕过 LLM** 直接调用提议工具。
    #   这样"建单必须经过确认流程"成为系统的硬保证。
    if pending is None:
        w_intent, cids, assignee = guard_rules.detect_write_intent_multi(
            state["question"])
        # 指代同样从上下文解析（与 node_agent 里的逻辑一致）
        if w_intent and not cids:
            for c in (state.get("context_in") or {}).get("customers") or []:
                if c.get("id"):
                    cids.append(str(c["id"]).upper())
        if w_intent and cids:
            logger.warning("agent: LLM 未产出待确认动作，启用确定性兜底 cids=%s", cids)
            if len(cids) > 1:
                r = tools.call_tool("propose_create_work_orders_batch",
                                    {"customer_ids": cids,
                                     "assignee": assignee or ""})
            else:
                r = tools.call_tool("propose_create_work_order",
                                    {"customer_id": cids[0],
                                     "assignee": assignee or ""})
            if r.get("ok"):
                pending = r["result"]
            else:
                logger.error("agent: 兜底建单提议也失败：%s", r.get("error"))

    if pending:
        return {"draft": _pending_text(pending), "pending_action": pending}

    # ── 零工具调用：决策节点的回答本身就是完整答案 ──────────────
    #
    # ⚠ 实测缺陷（用户报告「你好 → 暂无客户数据可分析」）：
    #   问「你好」时决策节点**已经答对了** ——
    #     「你好！我是银行客户流失预警系统的分析助手。我可以帮你做这些事：
    #       查客户风险 / 筛客户名单 / 查模型口径…」
    #   但本节点从头到尾**没读过它**，而是另叫写手（_WRITER_SYSTEM），
    #   喂给它一份 `tool_results = []` 的空 payload，让它重新编一句话。
    #   写手按自己的铁律（"只写你在数据里真实看到的"）看到空数据，
    #   于是写出「暂无客户数据可分析」—— **正确答案被丢掉，换成了一句
    #   像故障的兜底文案**。
    #
    #   这不是"缺关键词"：往 meta.py 加寒暄词表毫无作用，因为答案根本没走到
    #   那一步就被覆盖了。实测对照（docs/audit/exp-prompt-vs-enum.py）：
    #   纯提示词下模型对寒暄（含「吃了没」「嗯嗯」「哦」这类词表覆盖不到的
    #   说法）16/16 全对 —— 模型会答，是**下游把答案扔了**。
    #
    # ⚠ 为什么用 _needs_data 当闸门，这不算"退回枚举思维"：
    #   _needs_data 此前被用来**猜用户想干什么**（路由决策），那种用法必须
    #   穷尽所有说法，漏一个就出错，所以脆弱。
    #   这里它的角色完全不同 —— 只回答"**没有数据依据的回答能不能给用户看**"。
    #   这是**安全兜底**，方向上必须保守：
    #     · 命中（可能是数据问题）→ 不给模型的话，退回中性占位文案
    #       —— 保留 diag116 的防护：模型没查就说"没有待处理工单"这类
    #          无依据断言，不能展示
    #     · 未命中 → 说明这句话本就不该有数据依据（寒暄/身份/常识），
    #       展示模型的话
    #   即：误判只导致"少说一句"，不会导致"展示编造内容"。
    #
    # ⚠ 只影响 `tool_results` 为空的路径，不碰任何数据查询逻辑。
    if not state.get("tool_results"):
        decision_text = _last_decision_text(state)
        if decision_text and not _needs_data(state.get("question", "")):
            logger.info("agent: 零工具调用且非数据类问题，采用决策节点原答（不再叫写手重编）")
            # ⚠ 放 direct_answer 而**不是** headline：
            #   这是段落文本，前端 headline 是单行加粗标题样式，塞进去会畸形。
            #   且 answer_builder 零工具分支会让 headline 与 text 同值 →
            #   同一段话显示两遍（现存缺陷，本次一并修）。
            return {"draft": decision_text,
                    "direct_answer": decision_text,
                    "headline": "",
                    "insights": []}

    payload = json.dumps(state.get("tool_results", []),
                         ensure_ascii=False, default=str)
    if len(payload) > 12000:
        payload = payload[:12000] + "…（数据过长已截断）"

    msgs = [
        SystemMessage(content=_WRITER_SYSTEM),
        HumanMessage(content=(
            f"用户问题：{state['question']}\n\n"
            f"工具返回的真实数据（JSON）：\n{payload}\n\n"
            f"请按要求输出 JSON（headline + insights）。"
            f"再次强调：每个数字都必须来自上面的 JSON。"
        )),
    ]
    resp = llm.writer_llm().invoke(msgs)
    headline, insights = _parse_writer_output(resp.content)
    return {"draft": resp.content, "headline": headline, "insights": insights}


def _parse_writer_output(raw: str) -> tuple[str, list[str]]:
    """解析写手输出的 JSON。解析失败时**优雅降级**而不是报错。

    ⚠ 为什么要容错：模型偶尔会加 ```json 代码块或前后缀说明。
      这些都是无害的格式偏差，不该让整轮对话失败 ——
      解析不出时返回空值，由 answer_builder 用确定性文案兜底 headline，
      用户仍能看到完整的结构化卡片，只是少了一句模型写的话。
    """
    if not raw:
        return "", []
    text = raw.strip()
    # 去掉可能的 Markdown 代码块围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    # 截取第一个 { 到最后一个 }
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        text = text[i:j + 1]
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        logger.warning("agent: 写手输出非 JSON，降级为纯文本 headline")
        # 兜底：把整段当作 headline（截断），保证用户还能看到点东西
        return _cn_enums(raw.strip()[:80]), []
    headline = _cn_enums(str(obj.get("headline") or "").strip()[:120])
    ins = obj.get("insights")
    insights = []
    if isinstance(ins, list):
        for s in ins[:3]:
            s = _cn_enums(str(s).strip())
            if s:
                insights.append(s[:80])
    return headline, insights


# 枚举值 → 中文。⚠ 提示词已要求模型自己译，但**实测提示词不可靠**
# （本项目已在拒答、建单、序号三处栽过），故此处做**确定性替换**：
# 若模型仍夹带英文枚举，代码直接改掉，不依赖它自觉。
_ENUM_CN = {
    "CRITICAL": "极高", "HIGH": "高危", "MEDIUM": "中等", "LOW": "低风险",
    "ZERO": "零余额", "relationship": "客户经理", "outbound": "外呼",
    "automated": "自动触达",
}
# ⚠ 按长度倒序替换，避免 "HIGH" 先被 "HI" 之类误伤（当前无此情况，防御性）
_ENUM_RE = re.compile(
    r"\b(" + "|".join(sorted(_ENUM_CN, key=len, reverse=True)) + r")\b")


def _cn_enums(text: str) -> str:
    """把夹带的英文枚举值替换为中文。

    ⚠ 只替换**独立的英文单词**（\\b 边界），不会误伤
    `C071081`、`expected_value` 这类标识符（它们不是枚举值，
    且含下划线/数字边界，\\b 不会匹配到单词中间）。
    """
    if not text:
        return text
    return _ENUM_RE.sub(lambda m: _ENUM_CN[m.group(1)], text)


def node_verify(state: AgentState) -> dict:
    """⑤ 数字保全校验 + 构造结构化答案。

    ⚠ 校验对象**只有 LLM 写的那部分**（headline + insights），
      不再校验整篇 Markdown —— 因为客户卡片/指标明细现在由 Python 从
      工具返回值直接构造，那些数字天然可溯源，无需再查一遍，
      也避免了"表格序号被误判为孤儿"那类误杀（见 15.13）。

    ⚠ 即使校验不通过，**结构化部分照常输出**，只是丢掉 LLM 写的那几句。
      这比原设计（整段回答丢弃、回退纯文本模板）体验好得多：
      用户仍能看到完整卡片与按钮，只是少了模型的一句观察。
    """
    tool_results = state.get("tool_results", [])

    # 待确认写操作：文本由系统生成，不含 LLM 数字，跳过校验
    #
    # ⚠ 这里必须自己构造结构化答案，**不能**调 answer_builder.build()。
    #   原因（实测缺陷）：写路径上工具只调了 propose_create_work_order，
    #   它被 node_tools 归入「写操作」分支，**不进 tool_results** ——
    #   于是 build() 拿到空列表，返回那句兜底文案
    #   「未能取得数据，无法回答该问题」，而正文（客户/等级/余额/理由）
    #   被丢掉了。前端因此只看到按钮，看不到要确认的是什么。
    if state.get("pending_action"):
        pa = state["pending_action"]
        p = pa.get("payload") or {}
        text = _pending_text(pa)
        # 复用 answer_builder 的格式化函数，避免在 graph 里再写一套
        # （金额/百分比格式若两处各写一份，迟早会出现「同一数字两种写法」）
        yuan, pct, wan = (answer_builder._yuan, answer_builder._pct,
                          answer_builder._wan)
        rc, tc = answer_builder._RISK_CN, answer_builder._TIER_CN
        action = pa.get("action")
        cur = pa.get("current") or {}

        if action in ("delete_work_order", "update_work_order"):
            # 工单类操作：卡片主标识是 order_id，不是客户
            oid = p.get("order_id")
            st_cn = {"pending": "待处理", "in_progress": "处理中",
                     "completed": "已完成", "lost": "已流失"}
            headline = pa.get("summary") or "待确认操作"
            entities = [{
                "customer_id": cur.get("customer_id"),
                "order_id": oid,
                "surname": cur.get("customer_name"),
                "primary": {"label": "工单编号", "value_text": f"#{oid}",
                            "value_wan": f"#{oid}", "raw": oid},
                "secondary": [
                    {"label": "当前状态", "value": st_cn.get(cur.get("status"),
                                                             cur.get("status") or "—")},
                    {"label": "负责人", "value": cur.get("assignee") or "未指派"},
                ],
                "tags": [t for t in [st_cn.get(cur.get("status")),
                                     rc.get(cur.get("risk_level"))] if t],
                "action": "",
                "has_active_order": False,
                "can_create_order": True,
                "worthiness": {"verdict": None, "net_text": None, "label": None},
            }]
            facts = [{"label": "操作", "value":
                      "取消（删除）工单" if action == "delete_work_order" else "修改工单"}]
            if action == "delete_work_order":
                facts.append({"label": "可恢复", "value": "否 —— 删除不可逆"})
        else:
            headline = pa.get("summary") or "待确认操作"
            entities = [{
                "customer_id": p.get("customer_id"),
                "surname": p.get("customer_name"),
                "primary": {
                    "label": "期望挽回",
                    "value_text": yuan(p.get("expected_value_snapshot")),
                    "value_wan": wan(p.get("expected_value_snapshot")),
                    "raw": p.get("expected_value_snapshot"),
                },
                "secondary": [
                    {"label": "流失概率", "value": pct(p.get("probability"))},
                    {"label": "余额", "value": yuan(p.get("balance"))},
                ],
                "tags": [t for t in [
                    rc.get(p.get("risk_level")),
                    tc.get(p.get("value_tier_snapshot")),
                ] if t],
                "action": p.get("strategy") or "",
                "has_active_order": False,
                "can_create_order": True,
                "worthiness": {"verdict": None, "net_text": None, "label": None},
            }]
            facts = [{"label": "负责人", "value": p.get("assignee") or "（未指定）"}]

        ans = {
            "kind": "pending",
            "headline": headline,
            "warning": {
                "level": "warn" if action == "delete_work_order" else "info",
                # ⚠ 不写 Markdown 标记：前端不渲染 Markdown（实测 `**` 会原样露出）。
                #   前端另有 plain() 兜底，但源头也不该产生标记 —— 双重保障。
                "text": ("该操作需要你确认后才会执行。"
                         + ("删除不可恢复。" if action == "delete_work_order" else "")),
                "hint": "",
            },
            "entities": entities,
            "facts": facts,
            "actions": [],
            "insights": [],
            "text": text,
            "pending_action": pa,
        }
        return {"verify_passed": True, "verify_fail": [],
                "final": text, "answer": ans,
                "answer_basis": "system_pending_action",
                "citations": _citations(state)}

    # 只校验 LLM 写的文字
    llm_text = " ".join(
        [state.get("headline") or ""] + list(state.get("insights") or [])
    )
    ok, orphans = verify_mod.verify(
        llm_text, tool_results,
        # 上下文里的数字也算合法出处 —— 否则多轮追问引用上一轮数据
        # 会被误判为幻觉，徽章集体退化。详见 verify.verify 的说明。
        extra_sources=ctx_mod.numbers(state.get("context_in")),
    )

    headline = state.get("headline") or ""
    insights = state.get("insights") or []

    if not ok:
        # 丢弃模型写的文字，保留结构化数据。若模型确实编了数字，
        # 前端会在标题栏显示降级说明（见 answer_basis）。
        logger.warning("agent: 写手文字数字校验未通过，孤儿=%s", orphans)
        headline, insights = "", []

    # ── 徽章判据：只有「真的查到过数据」才配叫「数字已校验」 ──
    #
    # ⚠ 实测缺陷演进（两个阶段，都是同一个病）：
    #
    #   阶段一（diag116）：问「帮我取消已建单待处理状态客户」，模型没调任何
    #     工具就答「没有已建单待处理状态的客户」，而库里实际有 1 张 pending
    #     工单。它拿到的是 `llm_verified` —— 因为校验器只查"数字有没有出处"，
    #     一个数字都没有自然"通过"。
    #
    #   阶段二（diag126，当前修的就是这个）：我当时的修法是
    #        `if not retrieved and _needs_data(question)`
    #    即**依赖问题分类**。结果元问题（「你是谁」「可调用工具？」）被判为
    #    "不需要数据"，**完美绕过**该检查，照样拿到 llm_verified。实测
    #    「可调用工具？」答**"当前无可用工具"**，而系统实际有 9 个工具 ——
    #    又是"错误答案配高可信度徽章"。
    #
    #   根本问题：**用问题分类当判据必然有漏网**（分类表永远不全）。
    #   正确判据与问题无关：
    #       调过工具且成功 → 可以给 llm_verified
    #       零工具调用     → 一律不给（无论问的是什么）
    #
    #   这样把"问什么"从判据里彻底去掉 —— 不用维护关键词表，也不会再有
    #   漏网。代价是纯知识性提问（"什么是流失率"）也会被标为无依据，
    #   但那是**诚实**的：模型确实没有依据，它只是凭训练知识作答。
    retrieved = [t for t in tool_results if t.get("ok")]
    if not retrieved:
        logger.warning("agent: 零工具调用即作答，标记 no_data q=%s",
                       state.get("question", "")[:40])
        direct = (state.get("direct_answer") or "").strip()
        if direct:
            # ── 模型没查数据，但它的话本身就是完整答案 ──────────────
            # 场景：寒暄 / 问身份 / 问能力 / 常识。见 node_synthesis 的说明。
            #
            # ⚠ 仍然标 `no_data`，不升格为 `llm_verified`：
            #   它确实没有系统数据支撑，徽章必须如实。这是本项目的既定原则
            #   （"零工具调用 → 一律不给已校验"，见上方注释）。
            #   同时也**不加** warning —— 对"你好"这种正常回复挂一条
            #   "未经数据核对"的警示是噪音，会让用户以为出了问题。
            #   徽章本身已经承载了这个信息。
            ans = {
                "kind": "text",
                "headline": "",
                "warning": None,
                "entities": [], "facts": [], "actions": [], "insights": [],
                "text": direct,
            }
            return {
                "verify_passed": True,
                "verify_fail": [],
                "final": direct,
                "answer": ans,
                "answer_basis": "no_data",
                "citations": [],
            }
        ans = answer_builder.build(tool_results, state["question"],
                                   llm_headline=headline, llm_insights=insights)
        # 答案本身来自模型（可能是常识性回答），但**没有系统数据支撑**，
        # 故不冠以"已校验"，并明确告知用户这一点的含义。
        ans["warning"] = {
            "level": "info",
            "text": "本次回答未经系统数据核对（模型未查询任何数据）。",
            "hint": "若问的是客户/工单/阈值等业务数据，请换个问法重试。",
        }
        return {
            "verify_passed": ok,
            "verify_fail": orphans,
            "final": state.get("draft", ""),
            "answer": ans,
            "answer_basis": "no_data",
            "citations": [],
        }

    ans = answer_builder.build(
        tool_results, state["question"],
        llm_headline=headline, llm_insights=insights,
    )
    return {
        "verify_passed": ok,
        "verify_fail": orphans,
        "final": state.get("draft", ""),
        "answer": ans,
        "answer_basis": ("llm_verified" if ok else "llm_partial_dropped"),
        "citations": _citations(state),
    }


def node_fallback(state: AgentState) -> dict:
    """⑥ 兜底：把工具返回值直接构造为结构化答案（不经 LLM）。

    ⚠ 本条路径现在极少走到 —— `node_verify` 已经会在校验失败时
      **保留结构化卡片、只丢弃 LLM 写的文字**，因此"整篇丢弃"
      不再是首选。此节点保留给"verify 完全没能产出 answer"的异常情况
      （例如 answer_builder 抛错），作为最后一道保证：
      用户至少能看到工具返回的原始数据，而不是一片空白。
    """
    ans = answer_builder.build(state.get("tool_results", []), state["question"])
    note = verify_mod.explain(state.get("verify_fail", []))
    if note:
        ans["warning"] = {"level": "warn", "text": note, "hint": ""}
    return {"final": ans.get("text", ""), "answer": ans,
            "citations": _citations(state)}


# ══════════════════════════════════════════════════════════
# 模板回答（不经过 LLM 的兜底）
# ══════════════════════════════════════════════════════════

def _template_answer(state: AgentState) -> str:
    """把工具返回的数据直接排版成中文。所有数字都来自工具，零推算。"""
    lines: list[str] = []
    for tr in state.get("tool_results", []):
        name, data = tr.get("tool"), tr.get("data")
        if not tr.get("ok"):
            lines.append(f"**{name}** 查询失败：{data.get('error') if isinstance(data, dict) else data}")
            continue

        if name == "get_customer_risk" and isinstance(data, dict):
            w = data.get("worthiness") or {}
            lines.append(
                f"**客户 {data.get('customer_id')}（{data.get('surname')}）**\n"
                f"- 流失概率 {data.get('probability')}，风险等级 {data.get('risk_level')}\n"
                f"- 余额 ¥{data.get('balance'):,.0f}，价值层 {data.get('value_tier')}\n"
                f"- 期望价值 ¥{data.get('expected_value'):,.0f}\n"
                f"- 风险因素：{'、'.join(data.get('risk_factors') or []) or '无'}\n"
                f"- 建议动作：{data.get('action')}\n"
                + (f"- 经济性判定：{w.get('verdict')}（个体净收益 ¥{w.get('net'):,.0f}，"
                   f"盈亏平衡点 ¥{w.get('breakeven'):,.0f}）\n" if w else "")
            )
        elif name == "list_customers" and isinstance(data, dict):
            lines.append(f"**客户名单**（符合条件共 {data.get('matched_total')} 人，"
                         f"本次返回 {data.get('returned')} 人）")
            for c in data.get("items", []):
                lines.append(
                    f"- {c.get('customer_id')} {c.get('surname')}："
                    f"概率 {c.get('probability')}，余额 ¥{(c.get('balance') or 0):,.0f}，"
                    f"期望价值 ¥{(c.get('expected_value') or 0):,.0f}，"
                    f"等级 {c.get('risk_level')}"
                )
            if data.get("note"):
                lines.append(f"\n{data['note']}")
        elif name == "get_model_thresholds" and isinstance(data, dict):
            d = data.get("decision_metrics") or {}
            lines.append(
                f"**模型口径**（{data.get('model')}）\n"
                f"- 决策阈值 {data.get('decision_threshold')}，覆盖 {data.get('decision_coverage')}\n"
                f"- 分级线 {data.get('thresholds_grading')}\n"
                f"- 成本比 {data.get('cost_ratio')}，挽留成功率 {data.get('success_rate')}"
                f"（{data.get('success_rate_source')}）\n"
                f"- 测试集 precision {d.get('precision')}，recall {d.get('recall')}"
            )
        elif name == "get_business_summary" and isinstance(data, dict):
            lines.append(
                f"**成本收益推算**（口径：{data.get('basis')}）\n"
                f"- 客户总数 {data.get('total_customers')}，流失 {data.get('annual_churn_count')} 人"
                f"（{data.get('annual_churn_rate')}%）\n"
                f"- 假设客单价 ¥{data.get('avg_customer_value'):,.0f}，"
                f"单次干预成本 ¥{data.get('cost_per_intervention'):,.0f}\n"
                f"- 触达 {data.get('annual_flagged')} 人，投入 ¥{data.get('intervention_cost'):,.0f}\n"
                f"- 期望挽留 {data.get('expected_retained')} 人，"
                f"期望可挽回 ¥{data.get('expected_reduced_loss'):,.0f}，ROI {data.get('roi')}\n"
                f"- ⚠ {data.get('note')}"
            )
        elif name == "get_workorder_stats" and isinstance(data, dict):
            lines.append(
                f"**工单统计**\n"
                f"- 总计 {data.get('total')}：待处理 {data.get('pending')}、"
                f"处理中 {data.get('in_progress')}、已完成 {data.get('completed')}、"
                f"已流失 {data.get('lost')}\n"
                f"- 负责人：{'、'.join(data.get('assignees') or [])}"
            )
        else:
            lines.append(f"**{name}**\n```json\n"
                         f"{json.dumps(data, ensure_ascii=False, default=str)[:1500]}\n```")

    if not lines:
        lines.append("未能取得数据，无法回答该问题。请换个问法或稍后重试。")
    return "\n".join(lines)


def _pending_text(pending: dict) -> str:
    """写操作的待确认文本。由系统生成，不经 LLM —— 措辞不会出错。

    ⚠ 三种写操作分别措辞：
      create_work_order  建单
      update_work_order  改状态/负责人
      delete_work_order  取消（删除）
      不能共用一套文案 —— 用户必须一眼看清"这次要做的到底是什么"，
      尤其删除是不可逆的。
    """
    action = pending.get("action")
    p = pending.get("payload") or {}
    cur = pending.get("current") or {}

    if action == "delete_work_order":
        return (
            f"### ⏸ 待确认：取消工单\n\n"
            f"{pending.get('summary')}\n\n"
            f"**该操作需要你确认后才会执行。**\n\n"
            f"- 工单编号：#{p.get('order_id')}\n"
            f"- 客户：{cur.get('customer_id')}（{cur.get('customer_name')}）\n"
            f"- 当前状态：{cur.get('status')}\n"
            f"- 负责人：{cur.get('assignee') or '未指派'}\n\n"
            f"⚠ **删除不可恢复**，该工单将从系统中移除。\n\n"
            f"请点击下方按钮确认或取消。"
        )

    if action == "update_work_order":
        changes = []
        if p.get("status"):
            changes.append(f"状态 → {p['status']}")
        if p.get("assignee"):
            changes.append(f"负责人 → {p['assignee']}")
        if p.get("note"):
            changes.append("备注已更新")
        return (
            f"### ⏸ 待确认：修改工单\n\n"
            f"{pending.get('summary')}\n\n"
            f"**该操作需要你确认后才会执行。**\n\n"
            f"- 工单编号：#{p.get('order_id')}\n"
            f"- 客户：{cur.get('customer_id')}（{cur.get('customer_name')}）\n"
            f"- 当前状态：{cur.get('status')}\n"
            f"- 将要修改：{'；'.join(changes) or '（无变化）'}\n\n"
            f"请点击下方按钮确认或取消。"
        )

    # 默认：建单
    return (
        f"### ⏸ 待确认操作\n\n"
        f"{pending.get('summary')}\n\n"
        f"**该操作需要你确认后才会执行**，我不会直接建单。\n\n"
        f"- 客户：{p.get('customer_id')}（{p.get('customer_name')}）\n"
        f"- 风险等级：{p.get('risk_level')}，概率 {p.get('probability')}\n"
        f"- 余额：¥{(p.get('balance') or 0):,.0f}，价值层 {p.get('value_tier_snapshot')}\n"
        f"- 建议动作：{p.get('strategy')}\n"
        f"- 负责人：{p.get('assignee') or '（未指定）'}\n\n"
        f"建议理由：{p.get('note', '')[:400]}\n\n"
        f"请点击下方按钮确认或取消。"
    )


def _citations(state: AgentState) -> list[str]:
    """列出本次回答用到的数据来源 —— 供前端展示"依据"。"""
    out = []
    for tr in state.get("tool_results", []):
        if tr.get("ok"):
            out.append(f"tool://{tr['tool']}?{json.dumps(tr.get('args') or {}, ensure_ascii=False)}")
        else:
            out.append(f"tool://{tr['tool']}#failed")
    return out


def _tool_schemas() -> list[dict]:
    """把注册表转成 OpenAI function calling 的 schema。

    ⚠ 从 TOOL_SPECS 生成而非手写，避免"加了工具忘了加 schema"。
      参数表由各工具函数的签名与 docstring 约定，此处显式声明。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "get_customer_risk",
                "description": "查单个客户的流失风险详情：概率、等级、余额、价值层、"
                               "期望价值、风险因素、建议动作，以及个体经济性"
                               "（是否值得投入人工干预）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string",
                                        "description": "客户编号，如 C034525"},
                    },
                    "required": ["customer_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_customers",
                "description": "按条件查客户名单。返回前 limit 条，并如实给"
                               "符合条件总数。"
                               "⚠ 用户说「另外几个」「再调一批」「还有吗」时，"
                               "必须把 offset 设为上一次已返回的条数，"
                               "否则会返回完全相同的名单。"
                               "⚠ 「价值一般 / 中等价值」对应 value_tier=LOW。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "risk_level": {"type": "string",
                                       "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
                        "value_tier": {"type": "string",
                                       "enum": ["HIGH", "LOW", "ZERO"],
                                       "description": "价值层：HIGH=高价值(≥10万)、"
                                                      "LOW=中低价值(0~10万)、"
                                                      "ZERO=零余额。"
                                                      "「价值一般/中等价值」用 LOW"},
                        "min_balance": {"type": "number",
                                        "description": "余额下限（本地过滤）"},
                        "max_balance": {"type": "number",
                                        "description": "余额上限（本地过滤）"},
                        "sort_by": {"type": "string",
                                    "enum": ["expected_value", "probability", "balance"]},
                        "limit": {"type": "integer",
                                  "description": "返回条数，1~50，默认 10"},
                        "offset": {"type": "integer",
                                   "description": "跳过前几条。用户说「另外 N 个」时，"
                                                  "传上一次返回的条数（或上次结果里的 "
                                                  "next_offset）。不传会返回重复名单。"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_model_thresholds",
                "description": "查模型口径：决策阈值、分级线、成本比、挽留成功率、"
                               "测试集 precision/recall。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_business_summary",
                "description": "查成本收益推算：年度流失、期望挽留人数、"
                               "干预投入、ROI。全部是推算值。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_workorder_stats",
                "description": "查工单**数量统计**：各状态有多少张、负责人清单。"
                               "只给数量，不给具体工单。若要具体单子请用 list_work_orders。",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_work_orders",
                "description": "列出**具体工单**（含 order_id、客户、负责人、状态）。"
                               "回答「哪些工单待处理」「某人手上有哪些单」必须用它。"
                               "取消/修改工单前也要先用它拿到 order_id。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string",
                                   "enum": ["pending", "in_progress",
                                            "completed", "lost"],
                                   "description": "按状态筛选，留空为全部"},
                        "assignee": {"type": "string", "description": "按负责人筛选"},
                        "limit": {"type": "integer", "description": "1~50，默认 20"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_create_work_order",
                "description": "**提议**给**某一位**客户创建挽留工单"
                               "（不会立即执行，只生成待用户确认的动作）。"
                               "一次只能提一位；要给多位请用 "
                               "propose_create_work_orders_batch。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_id": {"type": "string"},
                        "assignee": {"type": "string",
                                     "description": "负责人姓名，可省略"},
                        "note": {"type": "string",
                                 "description": "备注，省略时系统自动生成建议理由"},
                    },
                    "required": ["customer_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_create_work_orders_batch",
                "description": "**提议**给**多位**客户批量创建挽留工单"
                               "（不会立即执行，只生成待用户确认的动作）。"
                               "用户说「把这三位建单」「给这几个客户建单」"
                               "或一次点名多位客户时用它 —— "
                               "逐个调用单数版本只会弹出一张确认单。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "客户编号列表，如 "
                                           '["C071081","C034525"]，最多 20 位',
                        },
                        "assignee": {"type": "string",
                                     "description": "统一负责人姓名，可省略"},
                    },
                    "required": ["customer_ids"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_update_work_order",
                "description": "**提议**修改工单的状态/负责人/备注（不会立即执行）。"
                               "用 order_id 指定工单，可先用 list_work_orders 查到。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "integer", "description": "工单编号"},
                        "status": {"type": "string",
                                   "enum": ["pending", "in_progress",
                                            "completed", "lost"]},
                        "assignee": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["order_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "propose_delete_work_order",
                "description": "**提议**删除工单（即「取消这张单」，不会立即执行）。"
                               "⚠ 不可逆，必须先用 list_work_orders 确认 order_id 正确。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "integer", "description": "工单编号"},
                    },
                    "required": ["order_id"],
                },
            },
        },
    ]


# ══════════════════════════════════════════════════════════
# 条件边
# ══════════════════════════════════════════════════════════

def route_after_guard(state: AgentState) -> str:
    """guard 之后：命中即结束，否则进入 LLM 决策。"""
    return "blocked" if state.get("guard_block") else "continue"


def route_after_meta(state: AgentState) -> str:
    """meta 之后：已给出确定性答案就结束，否则交给 LLM 决策。

    ⚠ 判据用 `answer` 是否存在，而不是重新调一次 meta.detect() ——
      避免"判断两次"导致两处结论不一致（本项目反复踩过的坑）。
    """
    return "done" if state.get("answer") else "agent"


def route_after_agent(state: AgentState) -> str:
    """LLM 决策之后：有工具调用则执行，否则进入措辞。

    ⚠ 轮次上界检查在此：超过 AGENT_MAX_TOOL_ROUNDS 就停止调工具，
      直接进入措辞 —— 否则模型可能反复调同一个工具陷入死循环。

    ⚠ 另一条重要规则（实测缺陷 diag116）：
      若模型**一次工具都没调**就要作答，而问题属于「需要查数据」的类型，
      则**强制再要一轮**，并在提示词里明确要求它调用工具。
      原因：实测问「帮我取消已建单待处理状态客户」，模型没调任何工具
      就答「没有已建单待处理状态的客户」，而库里实际有 1 张 pending 工单。
      更糟的是它拿到了 `llm_verified`（"数字已校验"）徽章 ——
      因为校验器只查"数字有没有出处"，一个数字都没有自然"通过"。
      即：**无依据的断言得了高可信度标记**，这是最危险的一类错误。
    """
    if state.get("rounds", 0) >= settings.AGENT_MAX_TOOL_ROUNDS:
        logger.warning("agent: 达到工具轮次上界 %s，停止调用",
                       settings.AGENT_MAX_TOOL_ROUNDS)
        return "synthesize"

    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage):
            if getattr(m, "tool_calls", None):
                return "tools"
            # 无工具调用 —— 判断是否该强制补一次查数据。
            # 只在 rounds==0（还从未查过数据）且尚未强制过时触发，
            # 因此最多走一次 force_tools，不会死循环。
            if (state.get("rounds", 0) == 0
                    and not state.get("force_tool")
                    and _needs_data(state.get("question", ""))):
                logger.warning("agent: 模型未调工具即作答，强制要求查数据 q=%s",
                               state.get("question", "")[:40])
                return "force_tools"
            return "synthesize"
    return "synthesize"


# 「需要查数据」的问题特征。命中则不允许模型凭记忆作答。
# ⚠ 这是**保守**列表：宁可多要一轮工具调用（多花 2 秒），
#   也不要让一句没有依据的断言拿到"数字已校验"的徽章。
_NEEDS_DATA_KW = [
    # 工单相关（实测出问题的那类）
    "工单", "待处理", "处理中", "已建单", "建单", "取消", "删除",
    "派给", "负责人", "跟进",
    # 客户相关
    "客户 C", "C0", "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9",
    "余额", "风险", "高危", "极高", "价值层", "期望价值", "概率",
    # 口径与汇总
    "阈值", "决策线", "成本比", "成功率", "召回", "精准",
    "多少", "几个", "哪些", "名单", "统计", "总数", "占比",
    # ── 取名单的常见说法（实测缺陷，用户报告）─────────────
    # 用户说「再帮我调出3个客户」时，旧表里只有「几个」，
    # 而用户写的是阿拉伯数字「3个」→ 一个关键词都不命中 →
    # 不强制查数据 → 模型凭记忆答「未查询到符合条件的客户」，
    # 而且拿到了「未经数据核对」的徽章（系统自己知道没查，却没拦住）。
    "调出", "调取", "列出", "列一下", "找出", "找几个", "来几个",
    "给我", "来一批", "换一批", "另外几个", "另外几个", "还有",
    "客户名单", "客户列表", "推荐客户", "候选",
]

# 数字 + 量词（"3个客户""5位""10名""三个"）—— 纯关键词表覆盖不了，
# 因为数字是无限的，且中文数字与阿拉伯数字都要认。
# ⚠ 中文数字必须一起覆盖：实测「另外三个」在只认阿拉伯数字时返回 False
#   （「3个」命中但「三个」不命中），而中文数字在口语里更常见。
_CN_NUM = "一二三四五六七八九十两半几"
_NUM_UNIT_RE = re.compile(
    rf"(?:\d+|[{_CN_NUM}]+)\s*(?:个|位|名|条|批)\s*(?:客户|人|名单)?"
    r"|另[外一二三四五六七八九十\d]*\s*(?:几|些)?\s*(?:个|位|名|批)?"
)


def _needs_data(question: str) -> bool:
    """判断问题是否需要查数据才能回答。

    用于拦住"不查数据就下断言"。纯知识性提问（如"什么是流失率"）
    不命中关键词，仍允许模型直接作答。
    """
    if not question:
        return False
    q = question.strip()
    if re.search(r"\bC\d{4,}\b", q, re.IGNORECASE):
        return True
    # 数字+量词（"3个客户"）—— 关键词表列不全无限的数字组合
    if _NUM_UNIT_RE.search(q):
        return True
    return any(kw in q for kw in _NEEDS_DATA_KW)


def route_after_tools(state: AgentState) -> str:
    """工具执行之后：
       · 有写操作待确认 → 直接措辞（不再让 LLM 继续调工具）
       · 用户要求取消/改单，且已查到 order_id，但还没提议 → 强制提议
       · 否则回到 agent 让模型判断够不够（ReAct 循环）
    """
    if state.get("pending_action"):
        return "synthesize"

    # ── 工单取消/改单的确定性补强（实测缺口 diag116）──────────
    # 用户问「帮我取消已建单待处理状态客户」时，模型会：
    #   1) 调 list_work_orders 拿到待处理工单（正确）
    #   2) 然后**直接作答"已列出/没有"**，不调 propose_delete_work_order
    # 结果用户看不到确认按钮，操作无从完成。
    # 这里用确定性规则补一刀：识别到取消意图 + 查到了未结工单 → 强制提议。
    q = state.get("question", "")
    if state.get("rounds", 0) <= 2 and _wants_cancel(q):
        done = {r["name"] for r in state.get("tool_calls_made", [])}
        if not ({"propose_delete_work_order", "propose_update_work_order"} & done):
            ids = _open_order_ids(state)
            if ids:
                # 只处理第一张（多张时让模型按用户意图选，不替它决定）
                logger.info("agent: 取消意图但未提议，强制 propose_delete_work_order")
                return "force_write"
    return "agent"


# 取消/删除工单的意图词
_CANCEL_KW = ["取消", "删除", "删掉", "撤销", "作废"]


def _wants_cancel(q: str) -> bool:
    return any(k in (q or "") for k in _CANCEL_KW)


def _open_order_ids(state: AgentState) -> list[int]:
    """从已执行的 list_work_orders 结果里取出未结工单的 order_id。"""
    ids: list[int] = []
    for tr in state.get("tool_results", []):
        if tr.get("tool") != "list_work_orders" or not tr.get("ok"):
            continue
        for o in (tr.get("data") or {}).get("items", []):
            if o.get("status") in ("pending", "in_progress"):
                oid = o.get("order_id") or o.get("id")
                if isinstance(oid, int) and oid not in ids:
                    ids.append(oid)
    return ids


def route_after_verify(state: AgentState) -> str:
    """校验之后的走向。

    ⚠ 这里改了语义（配合结构化答案）：
      原设计是「校验不过 → 整段丢弃 → 走 fallback 重新生成」。
      但现在 verify 已经会在校验失败时**保留结构化卡片、只丢弃 LLM
      写的那几句**，所以那条 fallback 路径不再需要 —— 再走一次只会
      把已构造好的答案覆盖成降级版本。

      现在只有一种情况走 fallback：**verify 没能产出 answer**
      （异常兜底），那时用工具返回值现构造一个。
    """
    if state.get("answer"):
        return "ok"
    return "fallback"


# ══════════════════════════════════════════════════════════
# 构图
# ══════════════════════════════════════════════════════════

def build_graph():
    """构造并编译状态机。

    ⚠ 每次调用都重新编译（编译很快）。不缓存在模块级，是为了让
      测试能干净地重建；生产路径由 run() 缓存，见下方 _compiled。
    """
    g = StateGraph(AgentState)

    g.add_node("guard", node_guard)
    g.add_node("meta", node_meta)
    g.add_node("agent", node_agent)
    g.add_node("force_tools", node_force_tools)
    g.add_node("force_write", node_force_write)
    g.add_node("tools", node_tools)
    g.add_node("synthesize", node_synthesis)
    g.add_node("verify", node_verify)
    g.add_node("fallback", node_fallback)

    g.set_entry_point("guard")
    g.add_conditional_edges("guard", route_after_guard,
                            {"blocked": END, "continue": "meta"})
    g.add_conditional_edges("meta", route_after_meta,
                            {"done": END, "agent": "agent"})
    g.add_conditional_edges("agent", route_after_agent,
                            {"tools": "tools", "synthesize": "synthesize",
                             "force_tools": "force_tools"})
    g.add_edge("force_tools", "agent")
    g.add_edge("force_write", "agent")
    g.add_conditional_edges("tools", route_after_tools,
                            {"agent": "agent", "synthesize": "synthesize",
                             "force_write": "force_write"})
    g.add_edge("synthesize", "verify")
    g.add_conditional_edges("verify", route_after_verify,
                            {"ok": END, "fallback": "fallback"})
    g.add_edge("fallback", END)

    return g.compile()


_compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled


def run(question: str, session_id: str = "default",
        context: dict | None = None) -> dict:
    """跑一轮对话，返回可直接序列化给前端的 dict。

    返回键：
      answer          结构化答案（前端按 kind 渲染，见 answer_builder）
      basis           口径：llm_verified / llm_partial_dropped /
                      guard_blocked / system_pending_action
      citations       依据（工具名 + 参数）
      pending_action  待确认写操作（有则前端显示确认按钮）
      tool_calls      本次调用的工具留痕
      verify_failed   被丢弃的孤儿数字（非空说明 LLM 编了数字）
      context         更新后的会话上下文（前端存下，下一轮回传）

    `context` 由前端回传，见 agent/context.py 顶部关于"为什么无状态"的说明。
    """
    ctx_in = ctx_mod.normalize(context)

    st = initial_state(session_id, question)
    st["context_in"] = ctx_in
    out = get_graph().invoke(st, {"recursion_limit": 20})

    # ── 合并本轮实体，产出下一轮要用的上下文 ──────────────────
    cur = ctx_mod.extract(out.get("tool_results") or [],
                          out.get("pending_action"))
    ctx_out = ctx_mod.merge(ctx_in, cur)

    return {
        # 结构化答案 —— 前端主要消费这个
        "answer": out.get("answer") or {},
        # 纯文本兜底 —— 兼容旧前端，也为排查提供可读内容
        "text": out.get("final") or "",
        "basis": out.get("answer_basis") or "unknown",
        "citations": out.get("citations") or [],
        "pending_action": out.get("pending_action"),
        "tool_calls": [
            {"name": r["name"], "args": r["args"], "ok": r["ok"],
             "error": r.get("error")}
            for r in out.get("tool_calls_made", [])
        ],
        "verify_failed": out.get("verify_fail") or [],
        "guard_topic": out.get("guard_topic"),
        # 会话上下文 —— 前端原样存下，下一轮 request 里带回来。
        # 这样后端**不需要任何进程内状态**，多 worker 天然一致。
        "context": ctx_out,
    }
