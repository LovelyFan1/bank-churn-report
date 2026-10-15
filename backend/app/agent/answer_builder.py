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
_TIER_CN = {"HIGH": "高价值", "LOW": "中低价值", "ZERO": "零余额"}
_VERDICT_CN = {
    "worth": "值得投入",
    "marginal": "盈亏边界",
    "not_worth": "不建议投入",
    "no_asset": "无资产可留",
}


# ── 主入口 ────────────────────────────────────────────────

def to_text(answer: dict) -> str:
    """公共兜底文本只由核验后的答案生成，不能返回写手原始草稿。"""
    parts = []
    def add(value):
        value = str(value or "").strip()
        if value and value not in parts:
            parts.append(value)
    add(answer.get("headline"))
    if answer.get("text"):
        add(answer["text"])
    else:
        for entity in answer.get("entities") or []:
            primary = entity.get("primary") or {}
            add(" ".join(str(value) for value in (
                entity.get("customer_id"), entity.get("surname"), primary.get("label"),
                primary.get("value_text")) if value))
            for item in entity.get("secondary") or []:
                add(f"{item.get('label', '')}：{item.get('value', '')}")
        for fact in answer.get("facts") or []:
            add(f"{fact.get('label', '')}：{fact.get('value', '')}")
    warning = answer.get("warning") or {}
    add(warning.get("text"))
    for insight in answer.get("insights") or []:
        add(insight)
    return "\n".join(parts)

def select_primary(tool_results: list[dict], question: str) -> dict | None:
    # 按用户任务选择主结果，辅助查询不能覆盖客户名单或业务问题。
    main = None
    successful = [tr for tr in tool_results if tr.get("ok")]
    preferred = []
    if "实际" in question and any(w in question for w in ("挽留", "回访")):
        preferred = ["get_retention_summary"]
    elif any(w in question for w in ("推荐", "名单", "调出", "另外", "再来", "找出")) or (
            any(tr['tool'] == 'list_customers' for tr in successful)
            and any(w in question for w in ('列出', '继续', '客户'))):
        preferred = ["list_customers"]
    elif any(tr['tool'] == 'get_customer_risk' for tr in successful) and any(
            w in question for w in ('风险', '概率', '详情', '余额')):
        preferred = ['get_customer_risk']
    elif "工单" in question:
        preferred = ["get_workorder_stats", "list_work_orders"] if any(w in question for w in ("统计", "多少", "总数")) else ["list_work_orders"]
    elif any(w in question for w in ("阈值", "决策线", "召回率")):
        preferred = ["get_model_thresholds"]
    elif any(w in question for w in ("收益", "ROI", "投入")):
        preferred = ["get_business_summary"]
    elif "客户" in question or any(tr["tool"] == "get_customer_risk" for tr in successful):
        preferred = ["get_customer_risk", "list_customers"]
    for name in preferred:
        main = next((tr for tr in reversed(successful) if tr["tool"] == name), None)
        if main:
            break
    if preferred and main is None:
        return None
    for tr in reversed(tool_results) if main is None else []:
        if tr.get("ok"):
            main = tr
            break

    return main


def build(tool_results: list[dict], question: str,
          llm_headline: str = "", llm_insights: list[str] | None = None) -> dict:
    """把工具返回构造成结构化答案。

    返回：
      kind        customer_list / customer_detail / thresholds / business /
                  workorders / pending / text
      headline    一句话结论（由主任务同一工具结果生成）
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

    main = select_primary(tool_results, question)
    successful = [tr for tr in tool_results if tr.get("ok")]

    if main is None:
        errs = [f"{t['tool']}: {(t.get('data') or {}).get('error')}"
                for t in tool_results if not t.get('ok')]
        return {
            "kind": "text",
            "headline": llm_headline or "查询失败",
            "warning": None, "entities": [], "facts": [], "actions": [],
            "insights": llm_insights or [],
            "text": "以下查询未成功：\n" + "\n".join(errs) if errs else "没有取得当前问题需要的主结果，请明确查询对象。",
        }

    name = main["tool"]
    data = main["data"] or {}
    # 主标题与卡片由同一结果生成，避免模型标题引用辅助查询中的客户。
    llm_headline = ""
    if name == "get_retention_summary":
        return {"kind": "business", "headline": "工单标记的挽留结果（未经回访验证）",
                "warning": {"level": "warn", "text": data.get("note") or
                    "包含演示工单，不能据此证明实际挽留效果。", "hint": ""},
                "entities": [], "actions": [], "insights": [],
                "facts": [{"label": "已结案", "value": str(data.get("total_completed", 0))},
                          {"label": "标记挽留", "value": str(data.get("retained", 0))},
                          {"label": "标记流失", "value": str(data.get("lost", 0))}],
                "text": "以上为工单结果标记，不代表独立验证的客户回访或增量收益。"}

    if name == "list_customers":
        return _from_customer_list(data, llm_headline, llm_insights)
    if name == "get_customer_risk":
        details = [tr["data"] for tr in successful if tr["tool"] == "get_customer_risk"]
        if len(details) > 1:
            details = list({item["customer_id"]: item for item in details}.values())
            answer = _from_customer_list({"items": details, "returned": len(details),
                "matched_total": len(details), "is_complete": True}, "", llm_insights)
            answer["facts"] = []
            for item in details:
                for fact in _from_customer_detail(item, "", None)["facts"]:
                    answer["facts"].append({**fact, "label": f"{item['customer_id']} · {fact['label']}"})
            return answer
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
                "label": "风险资产评分",
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
            "can_create_order": not has_order and c.get("exited") == 0,
            "worthiness": {
                "verdict": verdict,
                "net_text": _yuan(w.get("net")) if w else None,
                "label": _VERDICT_CN.get(verdict) if verdict else None,
            },
        }
        entities.append(entity)
        if not has_order and c.get("exited") == 0:
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
            "label": "风险资产评分",
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
        "can_create_order": not has_order and data.get("exited") == 0,
        "worthiness": {
            "verdict": verdict,
            "net_text": _yuan(w.get("net")) if w else None,
            "label": _VERDICT_CN.get(verdict) if verdict else None,
        },
    }

    facts = [
        {"label": "规则提示（非模型归因）", "value": "、".join(data.get("risk_factors") or []) or "无"},
        {"label": "建议渠道", "value": data.get("channel") or "—"},
    ]
    explanation = data.get("model_explanation") or {}
    if explanation.get("top_factors"):
        facts.append({"label": "模型归因（SHAP，非因果结论）", "value": "、".join(
            f"{factor['feature']}：{factor['impact']:+.4f}"
            for factor in explanation["top_factors"][:5])})
    elif explanation.get("error"):
        facts.append({"label": "模型归因不可用", "value": str(explanation["error"])})
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
    elif data.get("exited") == 0:
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
        pending / in_progress → 可取消（保留记录）
        completed / lost / cancelled → 不提供取消按钮
      这个判断由 Python 依据 status 做出，不交给 LLM ——
      否则它会提议一个语义上不成立的动作（如把已完成的再标记完成）。
    """
    items = data.get("items", [])
    entities = []
    actions = []

    from app.services.work_order_labels import STATUS_LABELS as _ST_CN, status_label, result_label

    for o in items:
        st = o.get("status")
        oid = o.get("order_id") or o.get("id")
        tags = [status_label(st)]
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
                {"label": "状态", "value": status_label(st)},
                {"label": "处理结果", "value": result_label(o.get('result'))},
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
    # 只有未结工单提供取消按钮，已结案工单不能再取消。
    open_ids = [ (o.get("order_id") or o.get("id"))
                 for o in items if o.get("status") in ("pending", "in_progress") ]
    if open_ids:
        if len(open_ids) == 1:
            actions.append({
                "id": "cancel_order",
                "label": f"取消工单 #{open_ids[0]}",
                "targets": open_ids,
                "needs_confirm": True,
                "note": "取消后停止跟进，保留工单与审计记录。",
            })
        else:
            actions.append({
                "id": "cancel_order",
                "label": f"取消这 {len(open_ids)} 张未结工单",
                "targets": open_ids,
                "needs_confirm": True,
                "note": "取消后停止跟进，保留工单与审计记录。",
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
        {"label": "已取消", "value": str(data.get("cancelled", 0))},
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
