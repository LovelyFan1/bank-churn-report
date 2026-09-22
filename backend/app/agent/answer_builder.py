"""结构化答案构造 —— Python 决定信息层次，LLM 只写文字。

**为什么需要这一层（用户实测反馈）**

原设计让 LLM 直接写 Markdown，结果：

  · 前端**没有 Markdown 渲染库**（package.json 实测只有 vue/axios/echarts/pinia/three），
    于是 `#` `**` `|` 全部裸露在界面上
  · 模型自作主张输出 **10 列表格**，每行 100+ 字符，眼球无法横向扫描
  · **最关键的警示被放在文末小字** ——「符合条件共 96,418 人，仅返回 3 条」
    这条会直接改变用户行动（他可能以为只有 3 个高危客户），却是脚注
  · 结论、数据、解读、提示四种信息**同权重**，没有层次

**核心主张**

既然"数字不经过 LLM"已经做到了，那"**数字的排版也不该经过 LLM**"。

本模块从工具返回值（确定性数据）直接构造前端可渲染的结构：

    entity    客户实体卡片：主指标加大、副指标次要、标签、动作、按钮状态
    warning   警示条：样本不完整之类，**提到结论下方**而非文末
    actions   动作按钮：目标由 Python 数出来，不由 LLM 描述
    insights  LLM 写的短句解读（唯一保留给模型的措辞部分）

**分工边界**

| 谁 | 做什么 | 为什么 |
|---|---|---|
| Python | entities / warning / actions | 结构化数据 → 组件渲染，层次由代码控制，零幻觉 |
| LLM | headline + insights | 模型增量价值最高的地方（观察与解释），仍过数字校验 |

⚠ 本模块**不发网络请求、不调 LLM**，是纯函数 —— 因此可单测，
且同样的工具返回必然产出同样的结构。
"""

import re
from typing import Any

# ── 金额/百分比格式（与 note_service 同风格，但不依赖它，保持 agent 包自洽）──

def _yuan(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"¥{float(v):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _pct(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _wan(v: Any) -> str:
    """大额金额用「万元」表达 —— 22 万比 220,080 更易扫读。"""
    if v is None:
        return "—"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(n) >= 10000:
        return f"{n / 10000:.1f} 万"
    return f"{n:,.0f}"


_RISK_CN = {"CRITICAL": "极高", "HIGH": "高危", "MEDIUM": "中等", "LOW": "低风险"}
_TIER_CN = {"HIGH": "高价值", "LOW": "低价值", "ZERO": "零余额"}
_VERDICT_CN = {
    "worth": "值得投入",
    "marginal": "盈亏边界",
    "not_worth": "不建议投入",
    "no_asset": "无资产可留",
}


# ── 主入口 ────────────────────────────────────────────────

def build(tool_results: list[dict], question: str,
          llm_headline: str = "", llm_insights: list[str] | None = None) -> dict:
    """把工具返回构造成结构化答案。

    返回：
      kind        customer_list / customer_detail / thresholds / business /
                  workorders / pending / text
      headline    一句话结论（优先用 LLM 写的，缺失时用确定性兜底）
      warning     警示（样本不完整等），无则 None
      entities    实体列表（客户卡片）
      facts       键值对（口径类回答用）
      actions     动作按钮
      insights    LLM 写的短句解读
      text        纯文本兜底（kind=text 或前端不认识的 kind 时使用）
    """
    if not tool_results:
        return {
            "kind": "text",
            "headline": llm_headline or "",
            "warning": None, "entities": [], "facts": [], "actions": [],
            "insights": llm_insights or [],
            "text": llm_headline or "未能取得数据，无法回答该问题。",
        }

    # 取最后一个成功的工具结果作为主结果（ReAct 可能调多个工具，
    # 最后一个通常是模型认为最能回答问题的那个）
    main = None
    for tr in reversed(tool_results):
        if tr.get("ok"):
            main = tr
            break

    if main is None:
        errs = [f"{t['tool']}: {(t.get('data') or {}).get('error')}"
                for t in tool_results]
        return {
            "kind": "text",
            "headline": llm_headline or "查询失败",
            "warning": None, "entities": [], "facts": [], "actions": [],
            "insights": llm_insights or [],
            "text": "以下查询未成功：\n" + "\n".join(errs),
        }

    name = main["tool"]
    data = main["data"] or {}

    if name == "list_customers":
        return _from_customer_list(data, llm_headline, llm_insights)
    if name == "get_customer_risk":
        return _from_customer_detail(data, llm_headline, llm_insights)
    if name == "get_model_thresholds":
        return _from_thresholds(data, llm_headline, llm_insights)
    if name == "get_business_summary":
        return _from_business(data, llm_headline, llm_insights)
    if name == "get_workorder_stats":
        return _from_workorders(data, llm_headline, llm_insights)
    if name == "list_work_orders":
        return _from_workorder_list(data, llm_headline, llm_insights)

    return {
        "kind": "text",
        "headline": llm_headline or "",
        "warning": None, "entities": [], "facts": [], "actions": [],
        "insights": llm_insights or [],
        "text": _fallback_text(main),
    }


# ── 各类型构造 ────────────────────────────────────────────

def _from_customer_list(data: dict, headline: str,
                        insights: list[str] | None) -> dict:
    """客户名单 → 实体卡片 + 批量建单按钮。

    ⚠ 关键：`has_active_order` 与 `worthiness` 由工具**确定性取回**，
      本函数据此决定按钮是否可点、批量人数是多少。
      这些都不经过 LLM —— 用户不会点到一个注定 409 的按钮。
    """
    items = data.get("items", [])
    entities = []
    buildable: list[str] = []

    for c in items:
        cid = c.get("customer_id")
        has_order = bool(c.get("has_active_order"))
        w = c.get("worthiness") or {}
        verdict = w.get("verdict")

        tags = []
        if c.get("risk_level"):
            tags.append(_RISK_CN.get(c["risk_level"], c["risk_level"]))
        if c.get("value_tier"):
            tags.append(_TIER_CN.get(c["value_tier"], c["value_tier"]))
        if verdict:
            tags.append(_VERDICT_CN.get(verdict, verdict))

        entity = {
            "customer_id": cid,
            "surname": c.get("surname"),
            # 主指标：决策依据，界面应放大显示
            "primary": {
                "label": "期望挽回",
                "value_text": _yuan(c.get("expected_value")),
                "value_wan": _wan(c.get("expected_value")),
                "raw": c.get("expected_value"),
            },
            "secondary": [
                {"label": "流失概率", "value": _pct(c.get("probability"))},
                {"label": "余额", "value": _yuan(c.get("balance"))},
            ],
            "tags": tags,
            "action": c.get("action") or "",
            # 按钮可用性 —— 由后端确定，前端不得自行推断
            "has_active_order": has_order,
            "can_create_order": not has_order,
            "worthiness": {
                "verdict": verdict,
                "net_text": _yuan(w.get("net")) if w else None,
                "label": _VERDICT_CN.get(verdict) if verdict else None,
            },
        }
        entities.append(entity)
        if not has_order:
            buildable.append(cid)

    # ── 警示：样本不完整 ────────────────────────────────────
    # ⚠ 这是本模块最重要的改动之一。原设计把这条放在文末小字，
    #   而它会直接改变用户行动（以为只有 3 个高危客户）。
    #   现由 is_complete 字段**确定性地**生成警示，不依赖模型"记得说"。
    warning = None
    total = data.get("matched_total")
    returned = data.get("returned")
    if data.get("is_complete") is False and total:
        warning = {
            "level": "warn",
            "text": f"这是部分名单：符合条件共 {total:,} 人，本次仅返回 {returned} 人。",
            "hint": "如需更多，可说「返回前 20 个」或加上筛选条件。",
        }
    if data.get("post_filtered"):
        filt = {"level": "info",
                "text": "本次含本地余额筛选（服务端不支持余额区间参数）。",
                "hint": ""}
        warning = warning or filt

    # ── 动作按钮 ───────────────────────────────────────────
    actions = []
    blocked = data.get("blocked_by_active_order") or []
    if buildable:
        label = (f"为这 {len(buildable)} 人建单"
                 if len(buildable) > 1 else f"为 {buildable[0]} 建单")
        actions.append({
            "id": "create_orders",
            "label": label,
            "targets": buildable,
            "needs_confirm": True,
            "blocked": blocked,
        })
    if blocked:
        # 已有工单的人单独说明 —— 不提的话用户会疑惑"为什么少了一个"
        names = "、".join(blocked)
        if actions:
            actions[0]["note"] = f"{names} 已有进行中的工单，已排除。"
        else:
            actions.append({
                "id": "none", "label": "无可建单对象",
                "targets": [], "needs_confirm": False,
                "note": f"{names} 均已有进行中的工单。",
            })

    # headline 兜底：LLM 没写时用确定性文案
    if not headline:
        headline = f"共 {returned} 位客户" + (
            f"（符合条件 {total:,} 人）" if total and returned != total else "")

    return {
        "kind": "customer_list",
        "headline": headline,
        "warning": warning,
        "entities": entities,
        "facts": [],
        "actions": actions,
        "insights": insights or [],
        "text": _list_text(entities, data),
    }


def _from_customer_detail(data: dict, headline: str,
                          insights: list[str] | None) -> dict:
    """单客户 → 一张卡片 + 单条建单按钮。"""
    w = data.get("worthiness") or {}
    verdict = w.get("verdict")
    has_order = bool(data.get("has_active_order"))

    tags = []
    if data.get("risk_level"):
        tags.append(_RISK_CN.get(data["risk_level"], data["risk_level"]))
    if data.get("value_tier"):
        tags.append(_TIER_CN.get(data["value_tier"], data["value_tier"]))

    entity = {
        "customer_id": data.get("customer_id"),
        "surname": data.get("surname"),
        "primary": {
            "label": "期望挽回",
            "value_text": _yuan(data.get("expected_value")),
            "value_wan": _wan(data.get("expected_value")),
            "raw": data.get("expected_value"),
        },
        "secondary": [
            {"label": "流失概率", "value": _pct(data.get("probability"))},
            {"label": "余额", "value": _yuan(data.get("balance"))},
        ],
        "tags": tags,
        "action": data.get("action") or "",
        "has_active_order": has_order,
        "can_create_order": not has_order,
        "worthiness": {
            "verdict": verdict,
            "net_text": _yuan(w.get("net")) if w else None,
            "label": _VERDICT_CN.get(verdict) if verdict else None,
        },
    }

    facts = [
        {"label": "风险因素", "value": "、".join(data.get("risk_factors") or []) or "无"},
        {"label": "建议渠道", "value": data.get("channel") or "—"},
    ]
    if w:
        facts.append({"label": "个体净收益", "value": _yuan(w.get("net"))})
        facts.append({"label": "盈亏平衡点", "value": _yuan(w.get("breakeven"))})

    cid = data.get("customer_id")
    actions = []
    if has_order:
        actions.append({
            "id": "none", "label": "已有进行中的工单",
            "targets": [], "needs_confirm": False,
            "note": f"{cid} 已有进行中的工单，需先处理完毕。",
        })
    else:
        actions.append({
            "id": "create_orders", "label": f"为 {cid} 建单",
            "targets": [cid], "needs_confirm": True, "blocked": [],
        })

    if not headline:
        headline = f"{cid}（{data.get('surname')}）"
    return {
        "kind": "customer_detail",
        "headline": headline,
        "warning": None,
        "entities": [entity],
        "facts": facts,
        "actions": actions,
        "insights": insights or [],
        "text": "",
    }


def _from_workorder_list(data: dict, headline: str,
                         insights: list[str] | None) -> dict:
    """工单列表 → 工单卡片 + 可执行动作（改状态 / 取消）。

    ⚠ 工单卡片与客户卡片**刻意不同**：工单的主标识是 `order_id`（不是客户），
      且每张单可执行的动作取决于其**当前状态**：
        pending / in_progress → 可"开始处理"、可"取消（删除）"
        completed / lost      → 已是终态，只能"重新处理"或删除
      这个判断由 Python 依据 status 做出，不交给 LLM ——
      否则它会提议一个语义上不成立的动作（如把已完成的再标记完成）。
    """
    items = data.get("items", [])
    entities = []
    actions = []

    _ST_CN = {"pending": "待处理", "in_progress": "处理中",
              "completed": "已完成", "lost": "已流失"}

    for o in items:
        st = o.get("status")
        oid = o.get("order_id") or o.get("id")
        tags = [_ST_CN.get(st, st or "—")]
        if o.get("risk_level"):
            tags.append(_RISK_CN.get(o["risk_level"], o["risk_level"]))

        entities.append({
            # 工单实体的主键是 order_id —— 卡片上要显眼，因为取消/改状态要用它
            "customer_id": o.get("customer_id"),
            "order_id": oid,
            "surname": o.get("customer_name"),
            "primary": {
                "label": "工单编号",
                "value_text": f"#{oid}",
                "value_wan": f"#{oid}",
                "raw": oid,
            },
            "secondary": [
                {"label": "状态", "value": _ST_CN.get(st, st or "—")},
                {"label": "负责人", "value": o.get("assignee") or "未指派"},
            ],
            "tags": tags,
            "action": o.get("strategy") or "",
            # 工单卡片的"可建单"语义改成"可操作"，前端据此置灰
            "has_active_order": st in ("completed", "lost"),
            "can_create_order": st in ("pending", "in_progress"),
            "worthiness": {"verdict": None, "net_text": None, "label": None},
            "expected_value": o.get("expected_value_snapshot"),
        })

    # ── 按状态汇总可执行的动作 ──────────────────────────────
    # ⚠ 只对**有意义的**状态给出按钮。已完成/已流失的单不该出现"取消"以外的
    #   状态动作 —— 那会误导用户去做语义上多余的操作。
    open_ids = [ (o.get("order_id") or o.get("id"))
                 for o in items if o.get("status") in ("pending", "in_progress") ]
    if open_ids:
        if len(open_ids) == 1:
            actions.append({
                "id": "cancel_order",
                "label": f"取消工单 #{open_ids[0]}",
                "targets": open_ids,
                "needs_confirm": True,
                "note": "取消＝删除该工单，不可恢复。",
            })
        else:
            actions.append({
                "id": "cancel_order",
                "label": f"取消这 {len(open_ids)} 张未结工单",
                "targets": open_ids,
                "needs_confirm": True,
                "note": "取消＝删除工单，不可恢复。",
            })

    if not headline:
        n = data.get("returned")
        headline = f"共 {n} 张工单" if n else "无符合条件的工单"

    answer = {
        "kind": "workorder_list",
        "headline": headline,
        "warning": None,
        "entities": entities,
        "facts": [],
        "actions": actions,
        "insights": insights or [],
        "text": "",
    }
    # 样本不完整提示（与客户列表同口径）
    total = data.get("matched_total")
    if data.get("is_complete") is False and total:
        answer["warning"] = {
            "level": "warn",
            "text": f"这是部分列表：符合条件的工单共 {total} 张，本次仅返回 {data.get('returned')} 张。",
            "hint": "如需更多，可说「列出前 50 张」。",
        }
    return answer


def _from_thresholds(data: dict, headline: str,
                     insights: list[str] | None) -> dict:
    """模型口径 → 键值对 + 两套阈值的区分说明。"""
    m = data.get("decision_metrics") or {}
    g = data.get("thresholds_grading") or {}
    facts = [
        {"label": "模型", "value": data.get("model") or "—"},
        {"label": "决策阈值", "value": str(data.get("decision_threshold")),
         "note": "成本最优切点，用于决定是否干预"},
        {"label": "覆盖率", "value": _pct(data.get("decision_coverage"))},
        {"label": "分级线", "value": (
            f"极高 {g.get('critical')} / 高危 {g.get('high')} / "
            f"中等 {g.get('medium')}"), "note": "分位数分级，用于贴标签"},
        {"label": "成本比", "value": str(data.get("cost_ratio"))},
        {"label": "挽留成功率", "value": _pct(data.get("success_rate")),
         "note": "假设值，非实测"},
        {"label": "精准率", "value": str(m.get("precision"))},
        {"label": "召回率", "value": str(m.get("recall"))},
        {"label": "测试集样本", "value": f"{m.get('sample_size'):,}"
         if m.get("sample_size") else "—"},
    ]
    if not headline:
        headline = f"当前决策阈值 {data.get('decision_threshold')}"
    return {
        "kind": "facts",
        "headline": headline,
        "warning": {
            "level": "info",
            "text": "本系统有两套阈值，语义不同，不可混用。",
            "hint": data.get("thresholds_explained") or "",
        },
        "entities": [], "facts": facts, "actions": [],
        "insights": insights or [],
        "text": "",
    }


def _from_business(data: dict, headline: str,
                   insights: list[str] | None) -> dict:
    facts = [
        {"label": "客户总数", "value": f"{data.get('total_customers'):,}"
         if data.get("total_customers") else "—"},
        {"label": "流失人数", "value": f"{data.get('annual_churn_count'):,}"
         if data.get("annual_churn_count") else "—"},
        {"label": "流失率", "value": f"{data.get('annual_churn_rate')}%"},
        {"label": "假设客单价", "value": _yuan(data.get("avg_customer_value")),
         "note": "业务假设值"},
        {"label": "单次干预成本", "value": _yuan(data.get("cost_per_intervention"))},
        {"label": "触达人数", "value": f"{data.get('annual_flagged'):,}"
         if data.get("annual_flagged") else "—"},
        {"label": "干预投入", "value": _yuan(data.get("intervention_cost"))},
        {"label": "期望挽留", "value": f"{data.get('expected_retained'):,} 人"
         if data.get("expected_retained") else "—"},
        {"label": "期望可挽回", "value": _yuan(data.get("expected_reduced_loss"))},
        {"label": "ROI", "value": str(data.get("roi"))},
    ]
    if not headline:
        headline = f"期望挽留 {data.get('expected_retained')} 人，ROI {data.get('roi')}"
    return {
        "kind": "facts",
        "headline": headline,
        "warning": {
            "level": "warn",
            "text": "以上为**推算值**，非实际业务结果。",
            "hint": data.get("note") or "",
        },
        "entities": [], "facts": facts, "actions": [],
        "insights": insights or [],
        "text": "",
    }


def _from_workorders(data: dict, headline: str,
                     insights: list[str] | None) -> dict:
    facts = [
        {"label": "总计", "value": str(data.get("total"))},
        {"label": "待处理", "value": str(data.get("pending"))},
        {"label": "处理中", "value": str(data.get("in_progress"))},
        {"label": "已完成", "value": str(data.get("completed"))},
        {"label": "已流失", "value": str(data.get("lost"))},
        {"label": "负责人", "value": "、".join(data.get("assignees") or []) or "—"},
    ]
    if not headline:
        headline = (f"共 {data.get('total')} 张工单，"
                    f"待处理 {data.get('pending')}、处理中 {data.get('in_progress')}")
    return {
        "kind": "facts",
        "headline": headline,
        "warning": None,
        "entities": [], "facts": facts, "actions": [],
        "insights": insights or [],
        "text": "",
    }


# ── 纯文本兜底（前端不认识的 kind 时用）────────────────────

def _list_text(entities: list[dict], data: dict) -> str:
    lines = []
    for e in entities:
        lines.append(
            f"{e['customer_id']} {e.get('surname')}："
            f"{e['primary']['label']} {e['primary']['value_text']}，"
            + "，".join(f"{s['label']} {s['value']}" for s in e.get("secondary", []))
        )
    if data.get("note"):
        lines.append(data["note"])
    return "\n".join(lines)


def _fallback_text(main: dict) -> str:
    import json
    body = json.dumps(main.get("data"), ensure_ascii=False, default=str)
    return f"**{main['tool']}**\n{body[:1500]}"
