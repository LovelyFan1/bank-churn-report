"""建单「建议理由」生成 —— 全系统唯一来源。

**这个模块在做什么**

工单的 `note` 字段此前**填充率 0%**（diag87 实测：62 条工单无一条填写）。
原因很简单：建单弹窗给了个空 textarea，写不写全凭自觉，而一线没时间写。

于是损失了两样东西：
  1) 事后复盘时，没人知道"当初为什么给这个人建单"
  2) 客户经理打电话前，要自己去翻风险因素、自己判断这个人值不值得打

本模块把**系统已经算出来的东西**拼成一句可直接使用的中文理由，
预填到建单弹窗。人可以改（`note` 仍是普通 TEXT 字段，无任何约束）。

**关键设计：这是模板拼装，不是 LLM 生成**

理由里出现的每一个数字都来自确定性计算：
    概率、余额、价值层、风险等级、期望价值   ← risk_scoring 全量打分结果
    命中风险因素                            ← risk_scoring._risk_factors
    个体净收益 / 盈亏平衡点 / 是否值得救     ← risk_scoring.worthiness
    推荐动作、渠道                          ← risk_scoring.recommend_action

因此它**不可能幻觉**：句子里没有一处是模型"想"出来的。
若日后接入 LLM 改写措辞，必须保持同一条铁律：
    LLM 只许重组已有数字，不许产生新数字。
（可用后校验强制：提取输出中所有数字，与输入比对，多一个即丢弃重试。）

**为什么把"值不值得救"也写进理由**

这是本模块与"生成一段客套话"的根本区别。
实测（diag88，96,418 行、决策线 0.60）：决策线内 6,582 人中，
2,001 人（30.4%）按个体经济是净亏的，剔除后总净收益反增 19,804,925 元。
而这两组人的**平均概率只差 2.1 个百分点**（0.7793 vs 0.8005）——
靠概率根本区分不出来。真正的区分变量是余额（净亏者 98.7% 为零余额层）。

所以理由里必须同时出现「会不会跑」和「跑了亏多少」两件事，
否则建单的人只看到一个高概率，就会继续去打那些无资产可留的客户。
"""

from app.config import settings
from app.services import risk_scoring

# ── 金额/比率格式化（本模块内部用，避免依赖前端工具）──────────

def _yuan(v: float) -> str:
    """元 → 带千分位。¥158,420"""
    return f"¥{v:,.0f}"


def build_reason(
    prob: float,
    balance: float,
    risk_level: str,
    value_tier: str,
    risk_factors: list | None = None,
    expected_value: float | None = None,
    action: str | None = None,
    channel: str | None = None,
    success_rate: float | None = None,
) -> dict:
    """生成建单建议理由。

    返回：
      note          建议理由全文（可直接存入 work_orders.note）
      worthiness    risk_scoring.worthiness() 的原始结果，供前端标色/提示
      basis         口径标注 —— 前端必须一并显示，否则读者无法判断
                    这句话是"算出来的"还是"猜的"
      components    组成这句话的原始数字，供前端做 tooltip 溯源

    ⚠ `basis` 不可省略。本系统的整个价值建立在"每个数字都可追溯"上，
      一句没有口径标注的理由，与 LLM 随口说的没有区别。
    """
    s = risk_scoring.RETENTION_SUCCESS_RATE if success_rate is None else float(success_rate)
    factors = [f for f in (risk_factors or []) if isinstance(f, str)]

    risk_label = risk_scoring.RISK_LABELS.get(risk_level, risk_level or "—")
    tier_label = risk_scoring.VALUE_TIER_LABELS.get(value_tier, value_tier or "—")

    ev = expected_value
    if ev is None:
        ev = (float(prob) * float(balance)) if (prob is not None and balance) else 0.0

    # ⚠ 把权威 EV 传进去，使「净收益」与「期望可挽回」严格同源。
    #   不传的话 worthiness 会用 prob×balance 现算，与 expected_value 字段
    #   差约 0.1 元（字段由未舍入概率算得）—— 同一句话里两个口径。
    w = risk_scoring.worthiness(prob, balance, success_rate=s, expected_value=ev)

    # ── 第 1 句：风险画像（回答"会不会跑"）──────────────────
    seg1 = (
        f"{risk_label}风险（流失概率 {prob * 100:.1f}%），"
        f"余额 {_yuan(balance or 0)}，属{tier_label}层。"
    )

    # ── 第 2 句：风险来源（回答"为什么"）────────────────────
    if factors:
        # 最多列 3 项 —— 超过 3 项在一句话里读不下去，且后几项贡献度已很低
        shown = factors[:3]
        tail = f"等 {len(factors)} 项" if len(factors) > 3 else ""
        seg2 = f"命中风险因素：{'、'.join(shown)}{tail}。"
    else:
        seg2 = "未命中显式风险因素，属模型综合评分判定。"

    # ── 第 3 句：经济性（回答"值不值得救"）─────────────────
    # 这是本模块的核心。四档分开措辞，不合并。
    #
    # ⚠ 量纲必须统一（自查修正）：本段所有比较一律用
    #     「期望可挽回（EV × s）」 vs 「单次干预成本 c」
    #   因为净值 = EV×s − c，两式同量纲，读者可直接相减。
    #   平衡点 ¥33,333 是定义在**期望价值 EV** 上的门槛（= c/s），
    #   拿 EV×s 去和 ¥33,333 比会得出"6,475 约等于 33,333"的荒谬说法
    #   （实测踩过：差额 5 倍却写"恰在附近"）。故此处只把 ¥33,333
    #   作为**换算后的门槛**括注，实际比较对象始终是 c。
    cost_per = risk_scoring.cost_per_intervention()
    recover = ev * s

    if w["verdict"] == "no_asset":
        # ⚠ 措辞约束（自查修正）：不得写"改走低成本渠道可省钱"之类的话。
        #   本系统的成本模型对三个渠道用的是**同一个** cost_per
        #   （AVG_CUSTOMER_VALUE / COST_RATIO），**不区分渠道成本** ——
        #   自动化渠道省成本是现实常识，但不是本模型算出来的结论，
        #   写进理由就构成"用模型的口吻说模型没说过的话"。
        seg3 = (
            f"⚠ 账户余额为 0，无可挽回资产（期望可挽回 {_yuan(recover)}）；"
            f"投入人工干预净亏 {_yuan(abs(w['net']))}。"
            f"建议核实是否属他行高净值客户后，再决定是否占用人工。"
        )
    elif w["verdict"] == "not_worth":
        seg3 = (
            f"⚠ 期望可挽回 {_yuan(recover)}，仅相当于单次干预成本 "
            f"{_yuan(cost_per)} 的 {w['ratio'] * 100:.0f}%，"
            f"按个体经济不建议投入人工"
            f"（期望价值需达 {_yuan(w['breakeven'])} 才收支平衡）。"
        )
    elif w["verdict"] == "marginal":
        seg3 = (
            f"期望可挽回 {_yuan(recover)}，为单次干预成本 {_yuan(cost_per)} 的 "
            f"{w['ratio'] * 100:.0f}%，处于盈亏边界，建议人工判断 —— "
            f"若掌握他行资产等额外信息，可提升优先级"
            f"（收支平衡需期望价值达 {_yuan(w['breakeven'])}）。"
        )
    else:
        seg3 = (
            f"期望可挽回 {_yuan(recover)}，为单次干预成本 {_yuan(cost_per)} 的 "
            f"{w['ratio'] * 100:.0f}%，值得投入。"
        )

    # ── 第 4 句：动作与渠道（回答"怎么做"）─────────────────
    parts = []
    if action:
        parts.append(f"建议动作：{action}")
    if channel:
        parts.append(f"触达渠道：{risk_scoring.CHANNEL_LABELS.get(channel, channel)}")
    seg4 = ("；".join(parts) + "。") if parts else ""

    note = seg1 + seg2 + seg3 + (seg4 or "")

    # ── 追加口径尾注 ────────────────────────────────────────
    # ⚠ 这一步不是"客套"。理由里出现了具体金额，就必须说明它是**推算**的，
    #   因为：s=0.30 是行业假设值（本系统无任何真实挽留结果数据），
    #   AVG_CUSTOMER_VALUE=50,000 是假设值，且与真实流失客户平均余额
    #   （实测 ¥90,960）偏差 81.9% —— 见 diag91。不标注等于默认它可信。
    note += (
        f"〔口径：概率/等级/因素来自模型实时打分；"
        f"金额按假设客单价 {_yuan(settings.AVG_CUSTOMER_VALUE)}、"
        f"假设挽留成功率 {s:.0%} 推算，非实际业务结果〕"
    )

    return {
        "note": note,
        "worthiness": w,
        "basis": "deterministic_template",
        "components": {
            "probability": round(float(prob or 0), 4),
            "balance": round(float(balance or 0), 2),
            "risk_level": risk_level,
            "value_tier": value_tier,
            "risk_factors": factors,
            "expected_value": round(float(ev), 2),
            "expected_recoverable": round(float(ev) * s, 2),
            "individual_net_profit": w["net"],
            "breakeven_expected_value": w["breakeven"],
            "cost_per_intervention": round(risk_scoring.cost_per_intervention(), 2),
            "success_rate": s,
            "success_rate_source": "assumption(settings.RETENTION_SUCCESS_RATE)",
            "avg_customer_value_source": "assumption(settings.AVG_CUSTOMER_VALUE)",
        },
    }
