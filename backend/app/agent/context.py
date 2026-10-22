"""会话上下文（短期记忆）—— 指针式，不存内容。

**为什么是"指针"而不是"记忆"**

本项目的实体是数据库主键（`C071081` / 工单 `#63`），可**确定性寻址**，
不存在"用户下一句会提到什么"的开放性问题。因此不需要向量检索、
不需要 mem0/Zep 那类记忆框架 —— 存一份编号清单即可。

于是 token 账完全不同：

    贴 3 轮原文      ~1500 token/轮，且随轮次线性增长，十几轮后必须截断
    滚动摘要         ~300 token/轮，且**会丢编号**（对本系统是致命的）
    本模块           ~100 token/轮，**恒定**，不随轮次增长

**服务端共享任务状态**

Router 将实体指针、筛选、排序与游标存入数据库，以用户名和 session_id 隔离。
版本号拒绝过期请求，短期处理租约拒绝同一会话并发；多个 worker 共享同一状态。
前端只负责展示和恢复版本，客户端数值不进入事实校验。

**为什么注入在 user message，而不是 system prompt 开头**

DeepSeek 的上下文缓存是自动的（前缀命中即折扣）。system prompt 与工具
定义是天然静态的，若把每轮都变的上下文插在**最前面**，前缀全变 →
缓存全废，比不缓存更亏。故上下文拼在用户问句前部，保住静态前缀。

**指代的边界：枚举输入会漏，校验输出才完备**

语言是无限的（"第二个""他""刚才那个""这客户""他们"…），枚举说法
永远追不上。但**输出是有限的**（数据库实体）。故本模块不做输入枚举，
只做两件事：

    1. 把已知实体清单交给 LLM，让它自行理解指代（通配）
    2. 校验它最终取用的编号是否在名单内（`allowed_customer_ids`）

名单外的编号一律拦在工具执行之前 —— 这样即便模型幻觉出一个
`C999999`，也进不了工具层。这是"边界内"的机械保证，不靠提示词自觉。

⚠ 本模块仍逐字段校验输入（见 `normalize`）；首次兼容的前端实体指针不携带可信数值，
  否则等于给用户开了一个提示词注入入口。
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

# 实体栈上限 —— 溢出淘汰最旧。20 条约 360 token，恒定。
# 取舍：用"无限回溯"换"成本恒定"。真需要回溯更早的对象，就得重新查。
MAX_ENTITIES = 20
MAX_DISPLAYED = 50

# ── 可信历史事实的容量与时效（E03.5）────────────────────────
#
# 为什么要有独立的上限：历史事实是"上一次查询留下的数值"，它解决的是
# 「刚才那个客户的风险资产评分是多少」这类追问。但它同时是一个**可信池**——
# 池子越大，模型把旧值当现值的风险越高。故按"回答批次 + 总条数 + 时间"
# 三重限制，全部用命名常量记录，边界可测。
MAX_FACT_ANSWERS = 10        # 最近 10 个回答
MAX_FACTS = 200              # 合计最多 200 条
FACT_TTL_SECONDS = 900       # 单条最多 15 分钟；且不超过会话有效期

# 首期只保留这三类数值 —— 风险等级、工单状态、列表总数等
# 必须来自当前可信结果，不能因为历史数字相同就放行（E03.1）。
_HISTORY_METRICS = {
    "probability": "fraction",
    "balance": "yuan",
    "expected_value": "yuan",
}
# 允许产生历史事实的工具 —— 只有"按客户查询"的只读工具。
_FACT_SOURCES = {"get_customer_risk", "list_customers"}

# 注入时最多渲染几条明细。超出只给条数提示。
# 分界线取 10，与 tools.list_customers 的默认 limit 对齐（不拍脑袋）。
RENDER_CAP = 10

# 接收前端上下文时的硬上限，防超大输入
MAX_INPUT_ITEMS = 40

# 只接受形如 C071081 的编号 —— 前端传来的其它字符串一律丢弃
_CID_RE = re.compile(r"^C\d{4,}$", re.IGNORECASE)

# 从问句里找用户**显式写出**的编号（这些永远放行，是新话题的信号）
_ID_RE = re.compile(r"(?<![A-Za-z0-9])C\d{4,}(?![A-Za-z0-9])", re.IGNORECASE)


def empty() -> dict:
    return {"customers": [], "orders": [], "displayed_customer_ids": [],
            "facts": []}


def explicit_ids(text: str) -> list[str]:
    """问句里显式出现的客户编号（去重保序，保留原大小写）。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in _ID_RE.findall(text or ""):
        u = m.upper()
        if u not in seen:
            seen.add(u)
            out.append(m)
    return out


def resolve_create_targets(question: str, ctx: dict | None) -> list[str]:
    """Resolve the complete user-authorized create scope; ambiguity never widens it.

    The displayed list defines ordinal/group references. The smaller detail cache
    is only a legacy fallback, and a singular reference needs a unique focus.
    Callers preparing a batch may split this list, but must retain all targets.
    """
    from app.agent import guard_rules, quantities

    intent = guard_rules.write_intents(question).get("create")
    if intent is None:
        raise ValueError("请明确指定要创建工单的客户；咨询或否定不会创建提议。")
    explicit = list(intent["customer_ids"])
    text = "，".join(intent["clauses"])
    ctx = normalize(ctx)
    displayed = ctx["displayed_customer_ids"] or [c["id"] for c in ctx["customers"]]
    positions = re.findall(r"第(" + quantities.TOKEN + r")\s*(?:位|个|名)", text)
    if positions:
        if len(positions) != 1:
            raise ValueError("请逐位指定客户编号；多个序号不会自动扩充为整批。")
        position = quantities.integer(positions[0])
        if not 1 <= position <= len(displayed):
            raise ValueError("序号不在当前展示名单中，请重新查询或指定编号。")
        targets = [displayed[position - 1]]
        if explicit and explicit != targets:
            raise ValueError("序号与明确客户编号不一致，请核对后重新指定。")
        return targets
    if explicit:
        return explicit
    first = re.search(r"前(" + quantities.TOKEN + r")\s*(?:位|个|名|人)", text)
    if first:
        size = quantities.integer(first[1])
        if not 1 <= size <= len(displayed):
            raise ValueError("人数超出当前展示名单，请重新查询或指定编号。")
        return displayed[:size]
    group = re.search(r"他们|她们|它们|这些|那些|上述|全部|所有|这批|那批|"
                      r"(?:这|那)(?:" + quantities.TOKEN + r"|几)\s*(?:位|个|名|人)", text)
    if group:
        if not displayed:
            raise ValueError("当前没有可确认的客户名单，请先查询或指定编号。")
        size = quantities.quantity(text)
        if size is not None and size != len(displayed):
            raise ValueError("指定人数与当前展示名单不一致，请使用序号或明确客户编号。")
        return displayed
    if re.search(r"他|她|这位|那位|这个客户|那个客户|该客户|刚才(?:那个|这位)?", text):
        focus = ctx.get("focus_customer_id")
        if focus:
            return [focus]
        if len(displayed) == 1:
            return displayed
    raise ValueError("当前建单对象不唯一，请指定一个序号、客户编号或完整名单。")


def normalize(raw: Any, *, trusted: bool = False) -> dict:
    """校验并裁剪上下文。

    ⚠ 这是**不可信输入**：前端可被篡改，用户也能直接改 localStorage。
      故每个字段都校验类型与长度，非法项静默丢弃而不是报错 ——
      上下文坏了不该让整轮对话失败（与前端 loadSession 的容错同理）。

    ⚠ `trusted` 只对**服务端自己写出再读回**的上下文为真（数据库里的
      session.context）。客户端回传的上下文一律 trusted=False ——
      历史事实是核验白名单的一部分，若能由客户端注入，等于给提示词注入
      开了一个"数字随便写"的后门（E03.6）。第一轮同样防，不能只在
      version>0 时防。
    """
    if not isinstance(raw, dict):
        return empty()

    def _num(v: Any) -> float | None:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return float(v)

    def _str(v: Any, n: int = 40) -> str:
        return v.strip()[:n] if isinstance(v, str) else ""

    customers: list[dict] = []
    raw_cus = raw.get("customers")
    for c in (raw_cus if isinstance(raw_cus, list) else [])[:MAX_INPUT_ITEMS]:
        if not isinstance(c, dict):
            continue
        cid = _str(c.get("id"), 20)
        if not _CID_RE.match(cid):
            continue          # 只认标准编号，挡住任意文本
        customers.append({
            "id": cid.upper(),
            "name": _str(c.get("name"), 30),
            "prob": _num(c.get("prob")),
            "balance": _num(c.get("balance")),
        })

    orders: list[dict] = []
    raw_ords = raw.get("orders")
    for o in (raw_ords if isinstance(raw_ords, list) else [])[:MAX_INPUT_ITEMS]:
        if not isinstance(o, dict):
            continue
        oid = o.get("order_id")
        if isinstance(oid, bool) or not isinstance(oid, int) or oid <= 0:
            continue
        orders.append({
            "order_id": oid,
            "customer_id": _str(o.get("customer_id"), 20),
            "name": _str(o.get("name"), 30),
            "status": _str(o.get("status"), 20),
        })

    focus = _str(raw.get("focus_customer_id"), 20).upper()
    result = {"customers": customers[-MAX_ENTITIES:], "orders": orders[-MAX_ENTITIES:],
              "focus_customer_id": focus if _CID_RE.fullmatch(focus) else None}
    displayed = raw.get("displayed_customer_ids")
    if not isinstance(displayed, list):
        displayed = [c["id"] for c in customers]
    result["displayed_customer_ids"] = list(dict.fromkeys(
        str(cid).strip().upper() for cid in displayed[:MAX_DISPLAYED]
        if isinstance(cid, str) and _CID_RE.fullmatch(cid.strip())))
    query = raw.get("list_query")
    if isinstance(query, dict):
        keys = {"risk_level", "value_tier", "min_balance", "max_balance", "sort_by",
                "limit", "offset", "next_offset", "exited", "has_order", "worth_only",
                "geography", "model_version", "seen_customer_ids"}
        result["list_query"] = {k: v for k, v in query.items() if k in keys}
        seen_ids = query.get("seen_customer_ids") or []
        result["list_query"]["seen_customer_ids"] = list(dict.fromkeys(
            cid.upper() for cid in seen_ids[:500]
            if isinstance(cid, str) and _CID_RE.fullmatch(cid)))
    now = time.time()
    facts: list[dict] = []
    if trusted:
        raw_facts = raw.get("facts")
        for item in (raw_facts if isinstance(raw_facts, list) else [])[:MAX_FACTS]:
            valid = _valid_fact(item, now)
            if valid:
                facts.append(valid)
    result["facts"] = _trim_facts(facts, now)
    return result


def _cust(d: dict) -> dict | None:
    """把工具返回的一条客户记录裁成指针 + 极简标签。

    ⚠ 只留 4 个字段。工具原始结果有 ~30 个字段（约 500 token），
      若整条进历史，查 3 个客户就是 1500 token 且**每轮重复付**。
      数字细节需要时重新查工具即可（本地 SQLite，不花 LLM token）。
    """
    cid = d.get("customer_id")
    if not cid:
        return None
    return {
        "id": str(cid).upper(),
        "name": d.get("surname") or d.get("customer_name") or "",
        "prob": d.get("probability"),
        "balance": d.get("balance"),
    }


def _valid_fact(raw: Any, now: float) -> dict | None:
    """校验一条历史事实 —— 形状、指标白名单、单位、来源、时效。"""
    if not isinstance(raw, dict):
        return None
    metric = raw.get("metric")
    if metric not in _HISTORY_METRICS:
        return None
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    entity = raw.get("entity_id")
    if not isinstance(entity, str) or not _CID_RE.fullmatch(entity.strip()):
        return None
    observed = raw.get("observed_at")
    if isinstance(observed, bool) or not isinstance(observed, (int, float)):
        return None
    observed = float(observed)
    # 客户端时间不能延长有效期：以**服务端当前时间**判断（E03.5）
    if observed > now + 5 or now - observed > FACT_TTL_SECONDS:
        return None
    if raw.get("source") not in _FACT_SOURCES:
        return None
    unit = raw.get("unit") or _HISTORY_METRICS[metric]
    if unit != _HISTORY_METRICS[metric]:
        return None
    version = raw.get("model_version")
    return {
        "entity_type": "customer",
        "entity_id": entity.strip().upper(),
        "metric": metric,
        "value": value,
        "unit": unit,
        "source": raw["source"],
        "observed_at": observed,
        "model_version": version if isinstance(version, str) else None,
    }


def _trim_facts(facts: list[dict], now: float | None = None) -> list[dict]:
    """按"最近 10 个回答 / 合计 200 条 / 单条 15 分钟"裁剪。"""
    now = time.time() if now is None else now
    valid = [f for f in (facts or []) if isinstance(f, dict)
             and now - float(f.get("observed_at") or 0) <= FACT_TTL_SECONDS]
    groups = sorted({int(float(f["observed_at"])) for f in valid}, reverse=True)
    keep = set(groups[:MAX_FACT_ANSWERS])
    valid = [f for f in valid if int(float(f["observed_at"])) in keep]
    valid.sort(key=lambda f: float(f["observed_at"]), reverse=True)
    # 同一 (实体, 指标) 只留最新一条 —— 旧值不该与新值同池共存（E03.7）
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for fact in valid:
        key = (fact.get("entity_id"), fact.get("metric"))
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
        if len(out) >= MAX_FACTS:
            break
    return out


def history_facts(ctx: dict | None, now: float | None = None) -> list[dict]:
    """本轮可引用的可信历史事实（已裁剪，附带可展示的时间）。"""
    now = time.time() if now is None else now
    facts = _trim_facts((ctx or {}).get("facts") or [], now)
    for fact in facts:
        try:
            fact["observed_at_iso"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(float(fact["observed_at"])))
        except (TypeError, ValueError, OSError):
            fact["observed_at_iso"] = ""
    return facts


def _facts_from_record(tr: dict, observed_at: float) -> list[dict]:
    """从**本轮成功的只读工具结果**里抽事实（服务端产物，可信）。"""
    tool = tr.get("tool")
    if tool not in _FACT_SOURCES or not tr.get("ok"):
        return []
    data = tr.get("data") or {}
    version = data.get("model_version")
    rows = data.get("items") if tool == "list_customers" else [data]
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("customer_id"):
            continue
        for metric, unit in _HISTORY_METRICS.items():
            value = row.get(metric)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)):
                continue
            out.append({
                "entity_type": "customer",
                "entity_id": str(row["customer_id"]).upper(),
                "metric": metric,
                "value": float(value),
                "unit": unit,
                "source": tool,
                "observed_at": observed_at,
                "model_version": version if isinstance(version, str) else None,
            })
    return out


def extract(tool_results: list[dict], pending_action: dict | None,
            observed_at: float | None = None) -> dict:
    """从**本轮**工具结果里抽出实体，供合并进上下文。

    ⚠ `replace_*` 标记的语义很重要，它对齐的是**界面**：
      本轮调了 `list_customers` → 前端卡片被整批替换 → 上下文也应整批替换，
      否则"第二个"会指到上一次查询的人。
      本轮只查了单个客户详情 → 前端只多/换一张卡 → 该客户置顶，列表其余保留。
    """
    customers: list[dict] = []
    orders: list[dict] = []
    replace_customers = False
    replace_orders = False
    focus_customer_id = None
    focus_changed = False

    observed_at = time.time() if observed_at is None else observed_at
    facts: list[dict] = []

    for tr in tool_results or []:
        if not tr.get("ok"):
            continue
        data = tr.get("data") or {}
        tool = tr.get("tool")
        facts.extend(_facts_from_record(tr, observed_at))

        if tool == "list_customers":
            replace_customers = True
            replace_orders = True
            focus_changed = True
            focus_customer_id = None
            for it in data.get("items") or []:
                if isinstance(it, dict):
                    c = _cust(it)
                    if c:
                        customers.append(c)

        elif tool == "get_customer_risk":
            # 单查**不**替换列表，只把这个人置顶
            c = _cust(data)
            if c:
                customers.append(c)
                focus_customer_id = c["id"]
                focus_changed = True
                replace_orders = True
                orders = []

        elif tool == "list_work_orders":
            replace_orders = True
            focus_changed = True
            focus_customer_id = data.get("filter_customer_id") or None
            for it in data.get("items") or []:
                if not isinstance(it, dict):
                    continue
                oid = it.get("order_id") or it.get("id")
                if isinstance(oid, int) and not isinstance(oid, bool):
                    orders.append({
                        "order_id": oid,
                        "customer_id": str(it.get("customer_id") or ""),
                        "name": it.get("customer_name") or "",
                        "status": it.get("status") or "",
                    })

    # 待确认动作也要进上下文 —— 否则「给他建单」确认后说「刚才那个」会断线
    pa = pending_action or {}
    p = pa.get("payload") or {}
    if pa.get("action") == "create_work_order":
        c = _cust({
            "customer_id": p.get("customer_id"),
            "surname": p.get("customer_name"),
            "probability": p.get("probability"),
            "balance": p.get("balance"),
        })
        if c:
            customers.append(c)
    elif pa.get("action") == "create_work_order_batch":
        replace_customers = True
        replace_orders = True
        focus_changed = True
        focus_customer_id = None
        for item in pa.get("items") or []:
            c = _cust({**item, "surname": item.get("customer_name")})
            if c:
                customers.append(c)
    elif pa.get("action") in ("delete_work_order", "update_work_order"):
        oid = p.get("order_id")
        if isinstance(oid, int) and not isinstance(oid, bool):
            cur = pa.get("current") or {}
            orders.append({
                "order_id": oid,
                "customer_id": str(cur.get("customer_id") or ""),
                "name": cur.get("customer_name") or "",
                "status": cur.get("status") or "",
            })
            replace_orders = True
            focus_changed = True
            focus_customer_id = str(cur.get("customer_id") or "") or None

    return {
        "customers": customers,
        "orders": orders,
        "facts": facts,
        "replace_customers": replace_customers,
        "replace_orders": replace_orders,
        "focus_changed": focus_changed,
        "focus_customer_id": focus_customer_id,
        "displayed_customer_ids": (pa.get("requested_customer_ids") or [c["id"] for c in customers[:MAX_DISPLAYED]])
            if replace_customers else None,
        "list_query": next(({
            **(tr.get("args") or {}),
            "next_offset": (tr.get("data") or {}).get("next_offset", 0),
            "model_version": (tr.get("data") or {}).get("model_version"),
            "seen_customer_ids": list(dict.fromkeys(
                ((tr.get("args") or {}).get("exclude_ids") or []) +
                [it["customer_id"] for it in (tr.get("data") or {}).get("items", [])
                 if it.get("customer_id")]))
        } for tr in reversed(tool_results or [])
            if tr.get("ok") and tr.get("tool") == "list_customers"), None),
    }


def merge(prev: dict, cur: dict) -> dict:
    """把本轮实体并进上一轮上下文，去重保序、溢出淘汰最旧。

    顺序语义：**新查询的在前**（与界面展示顺序一致）。
    故"第一个"始终指最近一次查询的第一项。
    """
    prev = prev or empty()

    def _dedup(items: list[dict], key: str) -> list[dict]:
        seen: set = set()
        res: list[dict] = []
        for it in items:
            k = it.get(key)
            if k is None or k in seen:
                continue
            seen.add(k)
            res.append(it)
        return res

    if cur.get("replace_customers"):
        cus = list(cur.get("customers") or [])
    else:
        cus = list(cur.get("customers") or []) + list(prev.get("customers") or [])
    cus = _dedup(cus, "id")[:MAX_ENTITIES]

    if cur.get("replace_orders"):
        ords = list(cur.get("orders") or [])
    else:
        ords = list(cur.get("orders") or []) + list(prev.get("orders") or [])
    ords = _dedup(ords, "order_id")[:MAX_ENTITIES]

    # 历史事实：本轮新值优先；同 (实体, 指标) 只留最新的一条。
    # ⚠ 事实与展示名单**分开保存** —— 保存 A 的历史值不会把 A 插回
    #   当前 B 的名单，也不改变"第几个"的写目标（E03.4）。
    facts = _trim_facts(list(cur.get("facts") or []) + list(prev.get("facts") or []))

    return {"customers": cus, "orders": ords, "facts": facts,
            "displayed_customer_ids": cur.get("displayed_customer_ids")
                if cur.get("displayed_customer_ids") is not None
                else prev.get("displayed_customer_ids", [c["id"] for c in cus]),
            "list_query": cur.get("list_query") or prev.get("list_query"),
            "focus_customer_id": cur.get("focus_customer_id") if cur.get("focus_changed")
            else prev.get("focus_customer_id")}


def render(ctx: dict | None) -> str:
    """渲染成可注入提示词的文本块（约 100 token）。

    ⚠ 文案**刻意不枚举**说法。枚举（"第一个/第二个/最后一个"）永远有漏，
      且会诱导模型只认这几种。这里只给清单 + 说明"用户可以任何说法指代"，
      把理解权交给模型，把**校验权**留给 `allowed_customer_ids`。
    """
    ctx = ctx or {}
    customers = ctx.get("customers") or []
    orders = ctx.get("orders") or []
    if not customers and not orders:
        return ""

    lines = ["[会话上下文]",
             "以下是你在本次会话中已经查询到的对象，供理解指代使用："]
    if ctx.get("list_query"):
        import json
        lines.append("上次名单筛选与分页：" + json.dumps(ctx["list_query"], ensure_ascii=False))
    if ctx.get("focus_customer_id"):
        lines.append(f"当前焦点客户：{ctx['focus_customer_id']}。‘他/该工单’只指此客户，不得使用其他客户的工单。")

    if customers:
        displayed = ctx.get("displayed_customer_ids") or [c["id"] for c in customers]
        lines.append("当前展示名单完整编号（序号用于指代，最多50位）：")
        lines.extend(f"   {i}. {cid}" for i, cid in enumerate(displayed, 1))
        lines.append("客户（按顺序，与界面展示顺序一致）：")
        for i, c in enumerate(customers[:RENDER_CAP], 1):
            bits = [f"{i}. {c.get('id')}"]
            if c.get("name"):
                bits.append(str(c["name"]))
            if isinstance(c.get("prob"), (int, float)):
                bits.append(f"概率 {float(c['prob']) * 100:.1f}%")
            if isinstance(c.get("balance"), (int, float)):
                bits.append(f"余额 ¥{float(c['balance']):,.0f}")
            lines.append("   " + "｜".join(bits))
        if len(customers) > RENDER_CAP:
            lines.append(f"   （另有 {len(customers) - RENDER_CAP} 个更早查询的客户未列出）")

    if orders:
        lines.append("工单（按顺序）：")
        for i, o in enumerate(orders[:RENDER_CAP], 1):
            bits = [f"{i}. 工单 #{o.get('order_id')}"]
            if o.get("name"):
                bits.append(str(o["name"]))
            if o.get("customer_id"):
                bits.append(str(o["customer_id"]))
            if o.get("status"):
                bits.append(f"状态 {o['status']}")
            lines.append("   " + "｜".join(bits))
        if len(orders) > RENDER_CAP:
            lines.append(f"   （另有 {len(orders) - RENDER_CAP} 张更早查询的工单未列出）")

    lines.append(
        "用户可能用「第一个」「第二个」「最后一个」「他」「她」「刚才那个」"
        "「这个客户」「他们」等**任何**说法指代上面的对象 —— 请结合对话自行判断，"
        "不要反过来要求用户重复编号。"
    )
    lines.append(
        "⚠ 上面的姓名与数值只是帮助识别对象的线索，可能已过时；"
        "回答涉及具体数值时**必须重新调用工具查询**。"
        "若用户的问题与上面的对象无关，请忽略本段。"
    )
    facts = (ctx or {}).get("facts") or []
    if facts:
        lines.append(
            "上一次查询留下的可信数值（仅可用于回答「刚才/上次」这类问题；"
            "用户问「现在/最新」时必须重新查询）：")
        for fact in facts[:6]:
            lines.append(f"   {fact.get('entity_id')} {fact.get('metric')}"
                         f"={fact.get('value')}（{fact.get('unit')}）")
    return "\n".join(lines)


def numbers(ctx: dict | None) -> list[float]:
    """上下文里出现过的数值 —— 供 verify 扩容。

    ⚠ 若不做这一步，会出现一个反效果：模型引用了上一轮的余额，
      而 verify 只认**本轮**工具结果，于是判为"无法溯源的数字" →
      徽章从「数字已校验」掉成「部分未溯源」。**记忆一上线，徽章集体退化。**
      故出处集合必须同时包含上下文里的数字。

    ⚠ 刻意**不**收集编号里的数字（C071081 → 071081）：那是标识符的一部分，
      加进去会无谓地放宽校验。
    """
    out: list[float] = []
    for c in (ctx or {}).get("customers") or []:
        for k in ("prob", "balance"):
            v = c.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
    return out


def allowed_customer_ids(ctx: dict | None,
                         question: str,
                         tool_results: list[dict] | None) -> set[str]:
    """本轮允许被引用的客户编号集合（大写）。

    三个来源，缺一不可：

      1. 会话上下文里的                —— 用户指代的对象（"第二个""他"）
      2. 问句里显式写出的              —— 新话题；显式编号永远放行
      3. **本轮工具已查到的**          —— 模型先 list 再 detail 是正常路径，
                                         查过的人必须能继续被操作

    ⚠ 第 3 条容易被漏掉：只放行 1、2 会导致"模型查完列表再查详情"被自己拦住。
    """
    allowed: set[str] = set()
    allowed.update((ctx or {}).get("displayed_customer_ids") or [])

    for c in (ctx or {}).get("customers") or []:
        if c.get("id"):
            allowed.add(str(c["id"]).upper())

    for cid in explicit_ids(question):
        allowed.add(cid.upper())

    for tr in tool_results or []:
        if not tr.get("ok"):
            continue
        data = tr.get("data") or {}
        tool = tr.get("tool")
        if tool == "list_customers":
            for it in data.get("items") or []:
                if isinstance(it, dict) and it.get("customer_id"):
                    allowed.add(str(it["customer_id"]).upper())
        elif tool == "get_customer_risk" and data.get("customer_id"):
            allowed.add(str(data["customer_id"]).upper())

    return allowed


def allowed_order_ids(ctx: dict | None,
                      tool_results: list[dict] | None) -> set[int]:
    """本轮允许被引用的工单编号集合（对称于 allowed_customer_ids）。

    ⚠ 工单**没有**"问句里显式写出"这个来源的口径问题：
      用户在问句里说「取消 63 号工单」时模型会先 list 再 propose，
      故沿用"上下文 + 本轮已查到"两个来源即可。
    """
    allowed: set[int] = set()

    for o in (ctx or {}).get("orders") or []:
        oid = o.get("order_id")
        if isinstance(oid, int) and not isinstance(oid, bool):
            allowed.add(oid)

    for tr in tool_results or []:
        if not tr.get("ok") or tr.get("tool") != "list_work_orders":
            continue
        for it in (tr.get("data") or {}).get("items") or []:
            if not isinstance(it, dict):
                continue
            oid = it.get("order_id") or it.get("id")
            if isinstance(oid, int) and not isinstance(oid, bool):
                allowed.add(oid)

    return allowed


def out_of_scope_msg(cid: Any, allowed: set[str]) -> str:
    """名单外编号的拒绝说明 —— 给**模型**看的，不是给用户看的。

    模型收到这条 error 后会重新决策：要么改用名单内的编号，
    要么向用户澄清。这正是"解析不出来时反问，而不是罢工"。

    ⚠ 分两种情况措辞，否则会给出自相矛盾的指示：
      名单**非空** → 用户多半在指代名单里的人，请改用名单内的编号
      名单**为空** → 没有任何依据，说明这是模型自己臆想的编号，
                     必须先去查名单，而不是"单独查询它"
    """
    if not allowed:
        return (
            f"编号 {cid} 没有任何依据：本次会话既没有查询过它，"
            f"用户也没有在问题里写出它。**不要凭印象使用任何客户编号。**"
            f"请先调用 list_customers 查出候选客户，再基于返回结果作答。"
        )
    sample = "、".join(sorted(allowed)[:10])
    more = f" 等 {len(allowed)} 个" if len(allowed) > 10 else ""
    return (
        f"编号 {cid} 不在本次会话已确认的对象范围内"
        f"（可用：{sample}{more}）。"
        f"如果用户是在指代上面清单里的人，请改用清单中的编号重新调用；"
        f"如果这是用户**明确写出**的新编号，请先用 get_customer_risk 单独查询它。"
        f"不要凭空使用未经查询的编号。"
    )


def out_of_scope_order_msg(oid: Any, allowed: set[int]) -> str:
    """名单外工单编号的拒绝说明（给模型看）。"""
    if not allowed:
        return (
            f"工单号 {oid} 没有任何依据：本次会话没有查询过任何工单。"
            f"**不要凭印象使用工单号。**"
            f"请先调用 list_work_orders 查到目标工单，再用返回的 order_id 操作。"
        )
    sample = "、".join(f"#{i}" for i in sorted(allowed)[:10])
    more = f" 等 {len(allowed)} 张" if len(allowed) > 10 else ""
    return (
        f"工单号 {oid} 不在本次会话已查到的工单范围内（可用：{sample}{more}）。"
        f"请先调用 list_work_orders 确认目标工单，再用返回的 order_id 操作。"
    )
