"""工具注册表 —— Agent 能对系统做的**全部**动作。

**设计原则：工具是"薄封装"，不含业务逻辑**

每个工具只做三件事：拼 URL、发请求、把响应裁剪成 LLM 好读的形状。
所有业务逻辑（阈值、成本模型、价值层、动作表）都留在既有 service 层，
工具**不得**重新实现它们 —— 否则 Agent 给出的数字会与页面不一致，
正是本项目一直在消除的「同一件事两个数」。

**只读/写分离（MVP 的关键约束）**

`_READ_TOOLS` 里的工具直接执行；`_WRITE_TOOLS` 里的工具**不在图内执行**，
而是返回 `pending_action` 交给前端确认。理由：建单是有副作用的操作，
Agent 不应独断。MVP 先用"返回待确认 + 前端按钮"实现，
二期再升级为 LangGraph 的 `interrupt()`（图内中断）。

**为什么用 HTTP 自调而不直接 import service**

看似绕，但换来三个好处：
  1. 工具返回的**就是接口返回**，与页面所见完全同源（口径天然一致）
  2. 每个工具都可独立用 curl 复现，便于排查
  3. 服务层重构不会静默改变 Agent 的行为

代价是多一次本机回环（实测 0.02~0.54s），可接受。

**⚠ 内部鉴权（引入登录系统后新增的必要一环）**

上面这个"HTTP 自调"的设计，在系统加上鉴权之后会**立刻坏掉**：
工具层的 urllib 请求不带任何令牌，于是全部被 401 拦下 ——
实测症状是「Agent 查不到任何客户 / 建单说客户不存在」，而接口本身正常。

故此处统一携带**内部令牌**（见 auth_service.internal_token）。
它只用于服务间调用，配合回环来源校验，不代表任何行员。
真正的写操作仍由用户令牌经 /api/agent/confirm 触发 —— 工具层只查询。
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# 后端自己的地址。容器内走 localhost（同一容器），
# 宿主调试时可用环境变量覆盖。
_BASE = os.environ.get("AGENT_SELF_BASE", "http://127.0.0.1:8000")
_TIMEOUT = 30


def _headers() -> dict:
    """内部调用统一请求头 —— 带内部令牌，否则会被鉴权中间件 401。

    ⚠ 延迟到调用时取（而不是模块加载时算），原因有二：
      1. settings 可能由测试在导入后覆盖
      2. 避免模块导入即依赖 settings，保持 tools 可独立 import
    """
    h = {"Accept": "application/json"}
    try:
        from app.services import auth_service
        h["Authorization"] = "Bearer " + auth_service.internal_token()
    except Exception:
        # 取不到令牌时**不静默放行** —— 请求会正常 401，
        # 从而在验证阶段暴露问题，而不是悄悄变成匿名调用
        pass
    return h


def _get(path: str, **params) -> Any:
    """调用本系统自己的 GET 接口。失败时抛异常，由上层转成 tool error。"""
    clean = {k: v for k, v in params.items() if v is not None}
    url = _BASE + path
    if clean:
        url += "?" + urllib.parse.urlencode(clean, doseq=True)
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode())


# ══════════════════════════════════════════════════════════
# 只读工具
# ══════════════════════════════════════════════════════════

def get_customer_risk(customer_id: str) -> dict:
    """查单个客户的流失风险详情。

    返回概率、等级、价值层、余额、期望价值、风险因素、建议动作与渠道，
    以及**个体经济性**（期望可挽回、个体净收益、盈亏平衡点、是否值得投入）。

    用这个回答「某个客户要不要打电话」「为什么这个人风险高」。
    """
    d = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}")
    # 裁剪：只保留 LLM 需要的字段，避免把 30 个字段全塞进上下文
    keep = ["customer_id", "surname", "geography", "age", "balance",
            "probability", "risk_level", "value_tier", "expected_value",
            "risk_factors", "channel", "action", "strategy", "reason",
            "has_active_order"]
    out = {k: d.get(k) for k in keep if k in d}
    # 补个体经济性 —— 与 note_service / worthiness 同一口径
    try:
        note = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}/suggested-note")
        out["worthiness"] = note.get("worthiness")
        out["suggested_note"] = note.get("note")
    except Exception as e:
        out["worthiness_error"] = f"{type(e).__name__}: {e}"
    return out


def list_customers(risk_level: str | None = None,
                   value_tier: str | None = None,
                   min_balance: float | None = None,
                   max_balance: float | None = None,
                   sort_by: str = "expected_value",
                   limit: int = 10,
                   offset: int = 0) -> dict:
    """按条件查客户名单。

    risk_level: CRITICAL / HIGH / MEDIUM / LOW
    value_tier: HIGH / LOW / ZERO
    sort_by:    expected_value（默认，按"值得投入多少"排）或 probability
    limit:      返回条数，1~50
    offset:     跳过前几条（用于「另外几个」「再要一批」）

    ⚠ **用户说「另外三个」「再调几个」「还有吗」时必须传 offset** ——
      传成上一次的 limit（例如上次取了 3 条，这次 offset=3）。
      不传的话会**返回一模一样的名单**，用户会以为系统坏了。
      实测缺陷（用户报告）：说「另外三个」时返回的还是原来那三位。

      offset + limit 都受后端约束：后端 page_size 上限 100，
      故 offset 超过 100 时本函数会如实说明"无法再往后翻"，
      而不是静默返回空列表。

    ⚠ 「价值一般 / 中等价值 / 普通客户」→ value_tier 传 **LOW**
      （本系统价值层只有三档：HIGH ≥10万 / LOW 0~10万 / ZERO 零余额）。
      不要因为"一般"听起来像中间档就臆造一个不存在的枚举值。

    ⚠ 只返回前 limit 条，并**如实告知总数**。不要假装这是全部 ——
      `/api/customers` 的 page_size 上限是 100，而决策线内有 6,582 人，
      任何"给我全部值得打的人"都无法一次返回。

    ⚠ min_balance / max_balance 在本系统**没有后端参数**（接口不支持），
      故此处用本地过滤实现，并会在返回里标注 `post_filtered: true`
      以免调用方误以为这是服务端筛选。

    ⚠ 返回的每一项都带 `has_active_order` 与 `worthiness`（见下）。
      这两个字段是**给界面用的**，由本函数从后端确定性地取回，
      不经过 LLM —— 前端据此把"已有工单"的人置灰、
      把"不值得救"的人标出来。若交给 LLM 转述，它可能漏掉或说错。
    """
    limit = max(1, min(int(limit or 10), 50))
    offset = max(0, int(offset or 0))
    # 后端只支持 sort_by ∈ {probability, expected_value, balance, age, credit_score}。
    # 传非法值会**静默回退**到 probability（实测），因此这里必须自己校验，
    # 否则 Agent 以为按期望价值排了、实际是按概率排的。
    allowed_sort = {"expected_value", "probability", "balance", "age", "credit_score"}
    if sort_by not in allowed_sort:
        sort_by = "expected_value"

    need_local = min_balance is not None or max_balance is not None

    # ── offset → 后端分页参数 ─────────────────────────────
    #
    # 后端不直接收 offset，只收 page / page_size（内部换算）。
    # ⚠ 两个硬约束必须在这里处理，否则会**静默返回错数据**：
    #   1) page_size ≤ 100
    #   2) 本地过滤（min/max_balance）在**取回之后**做，
    #      故分页窗口要按"过滤前"算 —— 否则翻页会漏掉/重复客户。
    #      做法：本地过滤时固定取一页足够大的窗口（100 条），
    #      用 offset 决定从第几个窗口开始。
    page_size_cap = 100
    if need_local:
        # 过滤场景：按 100 条一页往后翻，保证窗口不重叠
        fetch = page_size_cap
        page = offset // page_size_cap + 1
        skip_in_page = offset % page_size_cap
    else:
        fetch = limit
        page = offset // max(limit, 1) + 1
        skip_in_page = offset % max(limit, 1)

    if offset >= page_size_cap and not need_local:
        # 后端 page_size 上限 100 → 无法用单页表达超过 100 的偏移。
        # 不静默返回空列表，而是如实说明，让模型改用更精确的筛选条件。
        return {
            "items": [],
            "returned": 0,
            "offset": offset,
            "limit": limit,
            "note": (f"无法从第 {offset + 1} 位开始返回："
                     f"本接口单次最多取前 {page_size_cap} 条。"
                     f"请改用更精确的筛选条件（如限定 risk_level / value_tier），"
                     f"或分段多次查询。"),
            "is_complete": False,
        }

    d = _get("/api/customers", page=page, page_size=fetch,
             risk_level=risk_level, value_tier=value_tier,
             sort_by=sort_by, sort_order="desc")

    items = d.get("items", [])
    if need_local:
        if min_balance is not None:
            items = [c for c in items if (c.get("balance") or 0) >= min_balance]
        if max_balance is not None:
            items = [c for c in items if (c.get("balance") or 0) <= max_balance]
        # 本地过滤后再按页内偏移切
        items = items[skip_in_page:]

    items = items[:limit]

    keep = ["customer_id", "surname", "balance", "probability",
            "risk_level", "value_tier", "expected_value", "channel",
            "action", "has_active_order"]

    # ── 富化：补 worthiness（个体经济性） ─────────────────────
    # 逐条调 /suggested-note 会比批量端点慢，故用批量端点一次取回。
    # ⚠ 批量端点上限 200，此处 limit ≤ 50，安全。
    wmap: dict[str, dict] = {}
    ids = [c.get("customer_id") for c in items if c.get("customer_id")]
    if ids:
        try:
            q = "&".join(f"customer_ids={urllib.parse.quote(str(i))}" for i in ids)
            bd = _get(f"/api/customers/suggested-notes/batch?{q}")
            for it in bd.get("items", []):
                wmap[it["customer_id"]] = {
                    "verdict": (it.get("worthiness") or {}).get("verdict"),
                    "net": (it.get("worthiness") or {}).get("net"),
                    "ratio": (it.get("worthiness") or {}).get("ratio"),
                    "breakeven": (it.get("worthiness") or {}).get("breakeven"),
                }
        except Exception as e:
            # 富化失败不阻断主查询 —— worthiness 是增强信息，
            # 缺了它前端只是少一个标记，不该让整个名单查不出来
            wmap = {"__error__": f"{type(e).__name__}: {e}"}  # type: ignore

    out_items = []
    for c in items:
        row = {k: c.get(k) for k in keep if k in c}
        cid = c.get("customer_id")
        row["worthiness"] = wmap.get(cid)
        out_items.append(row)

    total = d.get("total")
    # 可建单人数：排除已有进行中工单的 —— 前端据此显示"为 N 人建单"
    buildable = [r["customer_id"] for r in out_items
                 if not r.get("has_active_order")]
    blocked = [r["customer_id"] for r in out_items
               if r.get("has_active_order")]

    return {
        "items": out_items,
        "returned": len(out_items),
        "matched_total": total,          # 符合筛选条件的总数
        # ⚠ 本次跳过了前 offset 条 —— 回传出去，让模型知道
        #   "这不是第一页"，也便于它算下一次该用多少 offset。
        "offset": offset,
        "limit": limit,
        # next_offset：模型要"再要一批"时直接用这个值，不必自己算
        # （避免它把 offset 和 limit 搞混，返回重复名单）
        "next_offset": offset + len(out_items),
        # is_complete 表达"这一轮的名单是否已覆盖全部符合条件的人"。
        # ⚠ 必须把 offset 算进去：从第 50 位取 10 条时，即使返回了 10 条，
        #   也远未取完 —— 旧实现只比 len 与 total，会误报"已完整"。
        "is_complete": (offset + len(out_items)) >= (total or 0),
        "sorted_by": sort_by,
        "post_filtered": need_local,     # True = 含本地过滤，非服务端
        # 供界面直接使用的确定性派生量（不经过 LLM）
        "buildable_customer_ids": buildable,
        "blocked_by_active_order": blocked,
        "note": (
            f"这是第 {offset + 1}~{offset + len(out_items)} 位，"
            f"符合条件共 {total} 条。如需继续，传 offset={offset + len(out_items)}。"
            if total and (offset + len(out_items)) < total else ""
        ),
    }


def get_model_thresholds() -> dict:
    """查当前模型的口径：决策阈值、分位数分级线、成本比、挽留成功率。

    用这个回答「阈值是多少」「为什么是这个数」「名单为什么这么大」。

    ⚠ 本系统有**两套阈值**，语义完全不同，回答时不得混用：
      thresholds          分位数分级线（P95/P70/P35），用于贴风险标签
      decision_threshold  成本最优的绝对概率切点，用于决定是否干预
    """
    d = _get("/api/model/risk-info")
    m = d.get("decision_metrics") or {}
    return {
        "model": d.get("model"),
        "thresholds_grading": d.get("thresholds"),
        "decision_threshold": d.get("decision_threshold"),
        "decision_coverage": d.get("decision_coverage"),
        "cost_ratio": d.get("cost_ratio"),
        "success_rate": d.get("success_rate"),
        "success_rate_source": d.get("success_rate_source"),
        "decision_metrics": {
            "precision": m.get("precision"),
            "recall": m.get("recall"),
            "f1_score": m.get("f1_score"),
            "tp": m.get("tp"), "fp": m.get("fp"), "fn": m.get("fn"),
            "sample_size": m.get("sample_size"),
        },
        "thresholds_explained": (
            "thresholds 是分位数分级线（用于贴标签）；"
            "decision_threshold 是成本最优决策线（用于决定是否干预）。"
            "两者不可混用。"
        ),
    }


def get_business_summary() -> dict:
    """查成本收益推算：年度流失、期望挽留人数、干预投入、ROI。

    ⚠ 全部是**推算值**（基于假设客单价与假设挽留成功率），
      不是实际业务结果。回答时必须带上这一点。
    """
    d = _get("/api/cost-benefit/summary")
    keep = ["total_customers", "annual_churn_count", "annual_churn_rate",
            "avg_customer_value", "cost_per_intervention", "annual_flagged",
            "intervention_cost", "annual_loss", "optimal_threshold",
            "model_recall", "model_precision", "success_rate",
            "tp_at_threshold", "expected_retained", "expected_reduced_loss",
            "roi", "basis", "note"]
    return {k: d.get(k) for k in keep if k in d}


def get_workorder_stats() -> dict:
    """查工单统计：各状态数量、负责人清单。

    用于回答「工单处理得怎么样」「谁手上有单」。

    ⚠ 只给**数量**，不给具体是哪些工单。若用户问"哪些工单待处理"
      "把待处理的列出来"，必须改用 list_work_orders —— 凭数量无法回答。
    """
    d = _get("/api/work-orders/stats")
    return {
        "total": d.get("total"),
        "pending": d.get("pending"),
        "in_progress": d.get("in_progress"),
        "completed": d.get("completed"),
        "lost": d.get("lost"),
        "assignees": d.get("assignees"),
        "status_meaning": {
            "pending": "待处理", "in_progress": "处理中",
            "completed": "已完成（已标记挽留结果）", "lost": "已流失",
        },
    }


def list_work_orders(status: str | None = None,
                     assignee: str | None = None,
                     limit: int = 20) -> dict:
    """列出具体工单（含编号、客户、负责人、状态）。

    status:   pending / in_progress / completed / lost，留空为全部
    limit:    返回条数，1~50

    用这个回答「哪些工单待处理」「李铭手上有哪些单」——
    以及**取消工单前的查找**：必须先拿到 order_id 才能提议取消。

    ⚠ 每一单都带 `order_id`。取消/删除工单必须用这个 id，
      不能靠客户编号推测（同一客户可能有多张历史工单）。
    """
    limit = max(1, min(int(limit or 20), 50))
    # 状态白名单校验 —— 传非法值后端会返回空列表而非报错，
    # 会让人误以为"没有这类工单"（与 sort_by 静默回退同类问题）
    allowed = {"pending", "in_progress", "completed", "lost"}
    if status and status not in allowed:
        status = None

    d = _get("/api/work-orders", page=1, page_size=limit,
             status=status, assignee=assignee)

    keep = ["id", "customer_id", "customer_name", "risk_level",
            "status", "result", "assignee", "strategy",
            "expected_value_snapshot", "created_at"]
    items = []
    for o in d.get("items", []):
        row = {k: o.get(k) for k in keep if k in o}
        # 把 id 同时以 order_id 暴露，避免 LLM 把 "id" 误解成客户 id
        row["order_id"] = o.get("id")
        items.append(row)

    total = d.get("total")
    return {
        "items": items,
        "returned": len(items),
        "matched_total": total,
        "is_complete": len(items) >= (total or 0),
        "filter_status": status,
        "filter_assignee": assignee,
        "status_meaning": {
            "pending": "待处理", "in_progress": "处理中",
            "completed": "已完成", "lost": "已流失",
        },
        "note": (f"仅返回前 {len(items)} 条，符合条件共 {total} 条。"
                 if total and len(items) < total else ""),
    }


# ══════════════════════════════════════════════════════════
# 写工具（MVP 不在图内执行，返回待确认动作）
# ══════════════════════════════════════════════════════════

def propose_create_work_order(customer_id: str,
                              assignee: str = "",
                              note: str = "") -> dict:
    """**提议**给某个客户创建挽留工单（不会立即执行）。

    返回一个待确认动作，前端展示后由用户点击确认才真正建单。

    用这个回答「帮我给这个人建单」「把这个客户派给小李」。

    note 省略时由系统按该客户实时数据自动生成建议理由
    （含风险画像、命中因素、值不值得救、建议动作）。

    ⚠ 一次只能提一位。要给**多位**客户建单（用户说"给这三位建单"），
      请改用 propose_create_work_orders_batch —— 否则会像实测缺陷那样
      只提议一位，用户以为系统丢了两张单。

    ⚠ 这是**写操作**，必须经用户确认。不要在回答里声称"已经建好了"。
    """
    # 先查客户，确认存在并取回建单所需字段 —— 避免开出无法执行的空单
    d = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}")
    payload = {
        "customer_id": d.get("customer_id"),
        "customer_name": d.get("surname"),
        "geography": d.get("geography"),
        "risk_level": d.get("risk_level"),
        "probability": d.get("probability"),
        "balance": d.get("balance"),
        "risk_factors": d.get("risk_factors") or [],
        "strategy": d.get("action") or d.get("strategy") or "",
        "assignee": assignee or "",
        "note": note or "",
        "channel": d.get("channel"),
        "value_tier_snapshot": d.get("value_tier"),
        "expected_value_snapshot": d.get("expected_value"),
    }
    if not note:
        try:
            sn = _get(f"/api/customers/{urllib.parse.quote(str(customer_id))}/suggested-note")
            payload["note"] = sn.get("note", "")
        except Exception:
            pass
    return {
        "__pending_action__": True,
        "action": "create_work_order",
        "summary": f"为客户 {payload['customer_id']}（{payload['customer_name']}）创建挽留工单"
                   + (f"，负责人 {assignee}" if assignee else ""),
        "payload": payload,
        "requires_confirmation": True,
    }


def propose_create_work_orders_batch(customer_ids: list[str],
                                      assignee: str = "") -> dict:
    """**提议**給**多位**客户创建挽留工单（不会立即执行）。

    customer_ids: 客户编号列表，如 ["C071081","C034525","C062858"]。
                  最多 20 位（与后端批量建单接口的上限一致）。

    用这个回答「帮我把这三位建单」「给这几个客户建单」「把这批人派给小李」
    —— 即用户**一次要求给多位客户建单**的情形。

    ⚠ 为什么必须有这个工具（实测缺陷，用户报告）：
      用户看到列表后说「帮我把这三位建立工单」，旧实现只能提议**一位**，
      确认面板上只跳出一张单，用户以为系统丢了另外两张。
      根因是 propose_create_work_order 的签名只接受单个 customer_id，
      模型即使想提三位也无处安放。

    ⚠ 返回的 payload 是**列表**（items），与单数版本结构不同。
      前端据此渲染"批量确认面板"（含负责人下拉），
      确认后调 POST /api/work-orders/batch 一次提交。

    ⚠ 这是**写操作**，必须经用户确认。不要在回答里声称"已经建好了"。
    """
    ids = [str(c).strip().upper() for c in (customer_ids or []) if str(c).strip()]
    # 保序去重
    seen: set[str] = set()
    ids = [c for c in ids if not (c in seen or seen.add(c))]
    if not ids:
        return {"__error__": "customer_ids 不能为空"}
    ids = ids[:20]

    items: list[dict] = []
    failed: list[dict] = []
    for cid in ids:
        try:
            d = _get(f"/api/customers/{urllib.parse.quote(cid)}")
        except Exception as e:
            failed.append({"customer_id": cid, "reason": f"查询失败：{e}"})
            continue
        if not d or not d.get("customer_id"):
            failed.append({"customer_id": cid, "reason": "客户不存在"})
            continue
        item = {
            "customer_id": d.get("customer_id"),
            "customer_name": d.get("surname"),
            "geography": d.get("geography"),
            "risk_level": d.get("risk_level"),
            "probability": d.get("probability"),
            "balance": d.get("balance"),
            "risk_factors": d.get("risk_factors") or [],
            "strategy": d.get("action") or d.get("strategy") or "",
            "assignee": assignee or "",
            "note": "",
            "channel": d.get("channel"),
            "value_tier_snapshot": d.get("value_tier"),
            "expected_value_snapshot": d.get("expected_value"),
            # 已有进行中工单的标记出来 —— 前端要显示"这张会被跳过"，
            # 而不是等提交后收到 409 才解释
            "has_active_order": bool(d.get("has_active_order")),
        }
        try:
            sn = _get(f"/api/customers/{urllib.parse.quote(cid)}/suggested-note")
            item["note"] = sn.get("note", "")
        except Exception:
            pass   # 理由生成失败不阻断提议
        items.append(item)

    if not items:
        return {"__error__": "没有任何可建单的客户："
                             + "；".join(f"{f['customer_id']}({f['reason']})"
                                        for f in failed[:3])}

    return {
        "__pending_action__": True,
        "action": "create_work_order_batch",
        "summary": f"为 {len(items)} 位客户批量创建挽留工单"
                   + (f"，负责人 {assignee}" if assignee else ""),
        "items": items,
        "failed": failed,
        "requires_confirmation": True,
    }


def propose_update_work_order(order_id: int,
                              status: str | None = None,
                              assignee: str | None = None,
                              note: str | None = None) -> dict:
    """**提议**修改工单（改状态/负责人/备注），不会立即执行。

    status: pending / in_progress / completed / lost

    用这个回答「把这张单标为已完成」「派给李铭」「取消这张待处理工单」。

    ⚠ "取消"在本系统的语义是：把工单**删除**或**改回非进行中状态**。
      若用户说"取消工单"，通常想要的是**删除**（不再跟进），
      故请用 propose_delete_work_order；若只是想停止处理，
      用本工具把 status 改成合适的值。
      拿不准时，先向用户确认要删除还是改状态。

    ⚠ 这是**写操作**，必须经用户确认。不要在回答里声称"已经改好了"。
    """
    d = _get(f"/api/work-orders/{int(order_id)}")
    return {
        "__pending_action__": True,
        "action": "update_work_order",
        "summary": (f"修改工单 #{order_id}"
                    f"（客户 {d.get('customer_name')}）"
                    + (f"，状态改为 {status}" if status else "")
                    + (f"，负责人改为 {assignee}" if assignee else "")),
        "payload": {
            "order_id": int(order_id),
            "status": status,
            "assignee": assignee,
            "note": note,
        },
        "current": {
            "status": d.get("status"),
            "assignee": d.get("assignee"),
            "customer_id": d.get("customer_id"),
            "customer_name": d.get("customer_name"),
        },
        "requires_confirmation": True,
    }


def propose_delete_work_order(order_id: int) -> dict:
    """**提议**删除工单（即"取消这张单"），不会立即执行。

    用这个回答「取消这张工单」「删掉这个待处理单」。

    ⚠ 删除是不可逆的。返回体里带 `current`（当前状态与客户），
      供前端在确认面板上把"要删的是哪一张"显示清楚 ——
      避免删错单。

    ⚠ 这是**写操作**，必须经用户确认。不要在回答里声称"已经删了"。
    """
    d = _get(f"/api/work-orders/{int(order_id)}")
    return {
        "__pending_action__": True,
        "action": "delete_work_order",
        "summary": (f"取消（删除）工单 #{order_id} —— "
                    f"客户 {d.get('customer_id')} {d.get('customer_name')}，"
                    f"当前状态 {d.get('status')}"),
        "payload": {"order_id": int(order_id)},
        "current": {
            "status": d.get("status"),
            "assignee": d.get("assignee"),
            "customer_id": d.get("customer_id"),
            "customer_name": d.get("customer_name"),
            "risk_level": d.get("risk_level"),
        },
        "requires_confirmation": True,
    }


# ── 注册表 ───────────────────────────────────────────────
# name 必须与 LLM 看到的 function name 一致。
# 新增工具时**只改这一处**，graph / tools / 前端能力清单三边都从这里读。
#
# ⚠ `label` 是**面向用户的中文说明**，与 `description`（取自 docstring，
#   给 LLM 看的、可能含实现细节）**分开维护**：
#     · description 可能写得很长或带内部术语，直接展示给用户不合适
#     · 用户要的是"我能用它做什么"，不是"这个函数内部怎么实现的"
#   两者同源于本表，因此不会出现"界面说的和实际工具不符"。
TOOL_SPECS = {
    "get_customer_risk": {
        "fn": get_customer_risk,
        "write": False,
        "label": "查单个客户的流失风险详情",
        "description": get_customer_risk.__doc__.strip().split("\n")[0],
    },
    "list_customers": {
        "fn": list_customers,
        "write": False,
        "label": "按条件查客户名单",
        "description": list_customers.__doc__.strip().split("\n")[0],
    },
    "get_model_thresholds": {
        "fn": get_model_thresholds,
        "write": False,
        "label": "查当前模型的口径：决策阈值、分位数分级线、成本比、挽留成功率",
        "description": get_model_thresholds.__doc__.strip().split("\n")[0],
    },
    "get_business_summary": {
        "fn": get_business_summary,
        "write": False,
        "label": "查成本收益推算：年度流失、期望挽留人数、干预投入、ROI",
        "description": get_business_summary.__doc__.strip().split("\n")[0],
    },
    "get_workorder_stats": {
        "fn": get_workorder_stats,
        "write": False,
        "label": "查工单统计：各状态数量、负责人清单",
        "description": get_workorder_stats.__doc__.strip().split("\n")[0],
    },
    "list_work_orders": {
        "fn": list_work_orders,
        "write": False,
        "label": "列出具体工单（含编号、客户、负责人、状态）",
        "description": list_work_orders.__doc__.strip().split("\n")[0],
    },
    "propose_create_work_order": {
        "fn": propose_create_work_order,
        "write": True,
        "label": "提议给**某一位**客户创建挽留工单（不会立即执行）",
        "description": propose_create_work_order.__doc__.strip().split("\n")[0],
    },
    "propose_create_work_orders_batch": {
        "fn": propose_create_work_orders_batch,
        "write": True,
        "label": "提议给**多位**客户批量创建挽留工单（不会立即执行）",
        "description": propose_create_work_orders_batch.__doc__.strip().split("\n")[0],
    },
    "propose_update_work_order": {
        "fn": propose_update_work_order,
        "write": True,
        "label": "提议修改工单（改状态 / 负责人 / 备注），不会立即执行",
        "description": propose_update_work_order.__doc__.strip().split("\n")[0],
    },
    "propose_delete_work_order": {
        "fn": propose_delete_work_order,
        "write": True,
        "label": "提议删除工单（即「取消这张单」），不会立即执行",
        "description": propose_delete_work_order.__doc__.strip().split("\n")[0],
    },
}

READ_TOOL_NAMES = [n for n, s in TOOL_SPECS.items() if not s["write"]]
WRITE_TOOL_NAMES = [n for n, s in TOOL_SPECS.items() if s["write"]]


def call_tool(name: str, args: dict) -> dict:
    """执行一个工具。write 类工具**不会真正写库**，只返回待确认动作。

    返回形状统一为：
      成功  {ok: True,  ...工具返回值}
      失败  {ok: False, error: "..."}
    统一形状让上层（verify / 前端）无需按工具分支处理。
    """
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return {"ok": False, "error": f"未知工具 {name}"}
    try:
        result = spec["fn"](**(args or {}))
        return {"ok": True, "result": result}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body}"}
    except TypeError as e:
        # 参数名/类型不对 —— 这是 LLM 抽槽出错，需明确告知以便它重试
        return {"ok": False, "error": f"参数错误：{e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
