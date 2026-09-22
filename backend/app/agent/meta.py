"""元问题（问系统自身）—— 确定性回答，不经过 LLM。

**为什么需要这一层（实测缺陷 diag126）**

用户实测问「可调用工具？」，Agent 回答 **「当前无可用工具」**，
而系统实际有 **9 个**工具。同类还有：

    你的技术栈是什么  → 当前无数据可分析
    你能做什么        → 当前无客户数据可分析
    你的底层是什么    → 我是银行客户流失预警分析助手（答非所问）

且**全部**标着「模型作答 · 数字已校验」—— 错误答案配最高可信度徽章。

**根因有三层**

  1. `_needs_data()` 只看业务词（工单/客户/阈值…），元问题一个都不命中
      → 不触发强制查数据，也不标无依据
  2. 提示词通篇要求「先查再答」「不要自己算」，模型于是把一切非数据
     问题都往「没有数据」上靠 —— 它以为自己在守规矩
  3. 系统**自己知道**答案（`/api/agent/capabilities` 就返回 9 个工具），
     却从没让模型看见

**修法：不靠模型，用确定性模板**

元问题的答案是可枚举的、且有唯一事实来源：

    能力清单 ← TOOL_SPECS（工具注册表，代码里就有）
    答不了的主题 ← guard_rules.known_topics()
    技术栈 ← 本文件写死，与实际依赖一致

因此**拼出来即可**，零幻觉可能，也不必花一次 LLM 调用。
这与 answer_builder 的思路一致：能确定性回答的，不交给模型。

**设计取舍：只答"有确定答案"的，不聊天**

闲聊（笑话、天气）与数学（1+1）**不**在这里生成客套回答，
而是明确说明职责范围。理由：编客套话要维护更多分支，且会让人误以为
它什么都能聊；而"这三件事我答不了"式的坦白，正是本系统的既定风格。
"""

import re

# ── 意图分类（确定性）────────────────────────────────────
#
# ⚠ 顺序有意义：先匹配"能力"再匹配"身份"。
#   因为「你能做什么」同时含"你"与"做"，若身份词在前会误判。

_ABILITY = [
    "能做什么", "能干什么", "有什么功能", "会什么", "支持什么",
    "有哪些工具", "可调用工具", "有什么工具", "能用哪些工具",
    "工具列表", "有哪些能力", "你的能力", "你能查什么",
    "可以查什么", "能查哪些", "有什么作用", "能帮我做什么",
    "怎么用你", "如何使用", "使用说明", "帮助", "help",
]

# ⚠ 顺序很重要（实测踩坑）：「你是什么模型」同时含"你是什么"（身份）
#   与"模型"（技术）。若身份词在前，会被误判为 identity，
#   答成"我是银行客户流失预警系统助手" —— 答非所问。
#   故把技术类放到身份类**之前**检查，并在技术词里显式列出"模型"相关说法。
_TECH = [
    "什么模型", "你是什么模型", "模型是哪个", "用的什么大模型",
    "底层模型", "底层是什么", "底层是啥", "技术栈", "技术方案",
    "怎么实现的", "什么框架", "框架", "架构", "langgraph", "langchain",
    "gpt", "大模型", "ai 模型", "ai模型",
]

_IDENTITY = [
    "你是谁", "你是什么", "你叫什么", "介绍一下你", "自我介绍",
    "你是什么角色", "你是干什么的",
]

# 明确不在职责范围的（闲聊/数学等）—— 不硬答，也不装故障
_OUT_OF_SCOPE = [
    "天气", "笑话", "讲故事", "唱歌", "吃饭", "你是谁家的",
    "股票", "彩票", "翻译", "写诗", "写代码",
]

# 数学算式的识别不靠单条正则（见 detect() 的说明）：
# 原先用 `^\s*[\d\s+\-*/×÷().]+\s*[=?？]?\s*$` 匹配，但
# 「1+1等于几」含中文，匹配不到。现改为"剔除数字/运算符/数量词后是否为空"。
_MATH_STRIP_RE = re.compile(r"[\s\d+\-*/×÷().=?？]|等于|得几|多少|几")


def detect(question: str) -> str | None:
    """识别元问题类型。

    返回 'ability' / 'identity' / 'tech' / 'out_of_scope' / None。
    None 表示不是元问题，交回正常流程。
    """
    if not question:
        return None
    q = question.strip()
    ql = q.lower()

    # 纯算式（1+1、2*3、"1+1等于几"）→ 不在范围
    #
    # ⚠ 实测踩坑：「1+1等于几」含中文"等于几"，纯 ASCII 正则匹配不到。
    #   故分两步：先看是否全由数字/运算符/中文数量词构成，
    #   再看是否含数字。这样 "1+1" 与 "1+1等于几" 都能命中。
    if any(c.isdigit() for c in q):
        if not _MATH_STRIP_RE.sub("", q).strip():
            return "out_of_scope"

    # ⚠ 检查顺序（实测踩坑）：**技术类必须在身份类之前**。
    #   「你是什么模型」同时含"你是什么"（身份词）与"模型"（技术词），
    #   身份在前会答成"我是银行客户流失预警分析助手" —— 答非所问。
    for kw in _ABILITY:
        if kw.lower() in ql:
            return "ability"
    for kw in _TECH:
        if kw.lower() in ql:
            return "tech"
    for kw in _IDENTITY:
        if kw.lower() in ql:
            return "identity"
    for kw in _OUT_OF_SCOPE:
        if kw in q:
            return "out_of_scope"
    return None


# ── 答案构造 ──────────────────────────────────────────────

_IDENTITY_TEXT = (
    "我是**银行客户流失预警系统的分析助手**。\n\n"
    "我的职责范围是查询和解释这个系统里的数据：客户流失风险、"
    "价值分层、干预策略、模型口径、工单管理。\n\n"
    "我不做与本系统无关的事（闲聊、通用问答、预测未来趋势）。"
)


def build_ability() -> dict:
    """能力清单 —— 直接从工具注册表生成，不可能与实现不符。

    ⚠ 用户要求：能力范围**只列可做项**，不展示"答不了"那一栏。
      故此处只输出 9 个工具的事实（6 查询 + 3 操作）。
      边界拦截逻辑**不受影响** —— guard_rules 仍在调用模型前拦下那三类
      问题，被问到时给的就是完整的拒答说明（含实测依据）。

    ⚠ 文案取自 `TOOL_SPECS[n]["label"]`，**不在这里再写一份**。
      本项目反复出现"同一件事两处定义"（阈值、ROI、策略…），
      用户可见的工具说明必须只有一个来源。
    """
    from app.agent import tools

    read_tools = list(tools.READ_TOOL_NAMES)
    write_tools = list(tools.WRITE_TOOL_NAMES)

    def _label(n: str) -> str:
        spec = tools.TOOL_SPECS.get(n) or {}
        return spec.get("label") or spec.get("description") or n

    facts = [{"label": f"查询 · {i + 1}", "value": _label(n), "note": n}
             for i, n in enumerate(read_tools)]
    facts += [{"label": f"操作 · {i + 1}", "value": _label(n),
               "note": n + "（需确认）"}
              for i, n in enumerate(write_tools)]

    return {
        "kind": "facts",
        "headline": f"我能查 {len(read_tools)} 类数据、提议 {len(write_tools)} 种工单操作",
        "warning": {
            "level": "info",
            "text": "写操作（建单 / 改单 / 取消）一律需要你确认后才会执行。",
            "hint": "",
        },
        "entities": [],
        "facts": facts,
        "actions": [],
        "insights": [],
        "text": "",
        "meta_kind": "ability",
    }


def build_tech() -> dict:
    """技术栈 —— 与实际依赖一致（可核对 requirements.txt 与代码 import）。

    ⚠ 措辞必须准确。本项目**没有**用 LangChain 的 Chain/AgentExecutor，
      只用了它的两个子包（消息类型与 OpenAI 客户端）；编排是自写的
      LangGraph 状态机。说成"用了 LangChain"会显得像套壳，也不准确。
    """
    facts = [
        {"label": "大模型", "value": "DeepSeek（deepseek-chat）",
         "note": "经 OpenAI 兼容协议接入，可换任意兼容端点"},
        {"label": "编排", "value": "LangGraph 状态机",
         "note": "自建 7 节点图：Guard → Agent ⇄ Tools → 措辞 → 校验"},
        {"label": "模型接入", "value": "langchain-openai",
         "note": "仅用 ChatOpenAI 客户端，未用 LangChain 的 Chain"},
        {"label": "消息类型", "value": "langchain-core",
         "note": "仅用消息数据结构"},
        {"label": "工具", "value": "自研薄封装（对真实 HTTP 端点）",
         "note": "保证 Agent 与页面同源口径"},
        {"label": "防幻觉", "value": "自研数字保全校验 + 拒答硬拦截",
         "note": "数字全部来自确定性计算，不经过模型推算"},
        {"label": "后端", "value": "FastAPI + SQLAlchemy + SQLite"},
        {"label": "前端", "value": "Vue 3 + Vite + ECharts"},
    ]
    return {
        "kind": "facts",
        "headline": "DeepSeek + LangGraph 状态机 + 9 个系统工具",
        "warning": {
            "level": "info",
            "text": "回答里的每个数字都来自系统计算，不经过模型推算。",
            "hint": "",
        },
        "entities": [],
        "facts": facts,
        "actions": [],
        "insights": [],
        "text": "",
        "meta_kind": "tech",
    }


def build_identity() -> dict:
    return {
        "kind": "text",
        "headline": "我是银行客户流失预警系统的分析助手",
        "warning": None,
        "entities": [], "facts": [], "actions": [], "insights": [],
        "text": _IDENTITY_TEXT.replace("**", ""),
        "meta_kind": "identity",
    }


def build_out_of_scope(question: str) -> dict:
    """闲聊/数学 —— 明确说明职责，不硬答也不装故障。"""
    return {
        "kind": "text",
        "headline": "这不在我的职责范围内",
        "warning": {
            "level": "info",
            "text": "我只回答本系统数据相关的问题（客户流失、干预策略、"
                    "模型口径、工单管理）。",
            "hint": "可以试试：「挽回价值最高的3个客户」「现在决策阈值是多少」"
                    "「有哪些工单待处理」。",
        },
        "entities": [], "facts": [], "actions": [], "insights": [],
        "text": "",
        "meta_kind": "out_of_scope",
    }


_BUILDERS = {
    "ability": None,        # 需要参数，单独处理
    "tech": build_tech,
    "identity": build_identity,
}


def answer(question: str) -> dict | None:
    """元问题 → 结构化答案。不是元问题返回 None。

    ⚠ 这是**纯函数**：不发网络、不调 LLM，因此可单测且行为完全可预测。
    """
    kind = detect(question)
    if kind is None:
        return None
    if kind == "ability":
        return build_ability()
    if kind == "out_of_scope":
        return build_out_of_scope(question)
    return _BUILDERS[kind]()
