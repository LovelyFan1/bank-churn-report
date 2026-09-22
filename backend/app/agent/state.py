"""AgentState —— LangGraph 图的唯一数据契约。

**为什么单独一个文件**

图里的节点通过共享 state 通信。若 state 定义散落在各节点里，就会出现
「节点 A 写 `result`、节点 B 读 `tool_result`」这类字段名漂移 —— 正是本项目
一直在消除的「同一件事两个名字」在 Agent 层的翻版。

故：**所有跨节点传递的数据都必须在 AgentState 里声明**，节点只能读自己
声明的键、写自己负责的键。节点的职责边界由字段归属体现：

    guard      写 guard_block（拒绝原因）
    router     写 intent / slots
    tool_node  写 tool_results / tool_calls_made
    synthesis  写 draft
    verify     写 verify_fail / final / citations

`add_messages` 是 LangGraph 提供的 reducer：同名键在新旧 state 之间做
**追加**而非覆盖，用于消息历史。其余键默认覆盖语义。
"""

from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class ToolCallRecord(TypedDict):
    """一次工具调用的留痕 —— 供"依据"展示与数字校验溯源。"""

    name: str
    args: dict
    ok: bool
    result: Any          # 失败时为 {"error": "..."}
    error: str | None


class AgentState(TypedDict, total=False):
    # ── 输入 ────────────────────────────────────────────
    session_id: str
    question: str

    # ── 会话上下文（短期记忆，指针式）─────────────────────
    # ⚠ 为什么放在 state 而不是进程内存：生产是 `uvicorn --workers 4`，
    #   进程内记忆会让 4 个 worker 各自为政，表现为"有时记得有时不记得"。
    #   故由**前端回传**，后端保持无状态。详见 agent/context.py 顶部说明。
    #
    # context_in   上一轮结束后前端回传的实体清单（未经校验的原始 dict）
    # context_out  本轮结束时合并出的新清单，随响应返回给前端保存
    context_in: dict
    context_out: dict

    # ── 对话历史（reducer 追加语义）─────────────────────
    messages: Annotated[list, add_messages]

    # ── ① guard：拒答拦截 ───────────────────────────────
    # 非 None 表示命中硬拦截，图直接走向 END，不调 LLM。
    # ⚠ 这是**确定性**判断，不依赖 LLM 自觉 —— 实测 LLM 会被
    #   "哪个策略更好"这类问题误导去调无关工具（diag103）。
    guard_block: str | None
    guard_topic: str | None      # 命中的主题，供前端区分展示

    # ── ② router：意图与槽位 ────────────────────────────
    intent: str | None
    slots: dict

    # ── ③ tool_node：工具执行 ───────────────────────────
    tool_calls_made: list[ToolCallRecord]
    tool_results: list[dict]
    rounds: int                  # 已进行的工具轮次，用于循环上界
    force_tool: bool             # 是否已强制要求过调工具（防重复强制）
    # 强制调用某个写工具（如用户要取消工单但模型只查不操作）。
    # 置位后避免再次进入 force_write，防止死循环。
    force_write_tool: str | None
    # 待人工确认的写操作（如建单）。非 None 时图直接走向措辞，
    # **不会**真正执行 —— 由前端展示确认按钮，用户点击后另行调用接口。
    pending_action: dict | None

    # ── ④ synthesis：措辞生成 ──────────────────────────
    # draft     写手原始输出（可能是 JSON 文本，仅供排查）
    # headline  一句话结论（LLM 写，经校验）
    # insights  短句解读（LLM 写，经校验）
    # ⚠ 客户卡片/指标明细/按钮**不在这里** —— 那些由 answer_builder
    #   从 tool_results 直接构造，见该模块说明。
    draft: str
    headline: str
    insights: list[str]

    # ── ⑤ verify：数字保全校验 ─────────────────────────
    verify_fail: list[str]       # 无法溯源的数字
    verify_passed: bool

    # ── 输出 ───────────────────────────────────────────
    final: str
    answer: dict                 # 结构化答案（前端直接渲染）
    citations: list[str]
    # 回答的口径来源：llm_verified / llm_fallback_template /
    # guard_blocked / disabled，供前端显示"这句话是怎么来的"
    answer_basis: str


def initial_state(session_id: str, question: str) -> AgentState:
    """构造一轮对话的初始 state。

    ⚠ 用构造函数而非散落的 dict 字面量：新增字段时只需改这里一处，
      否则各调用点会漏填，而 TypedDict 在运行时**不校验**缺失键 ——
      漏填的后果是节点拿到 None 而不是报错。
    """
    return AgentState(
        session_id=session_id,
        question=question,
        context_in={},
        context_out={},
        messages=[],
        guard_block=None,
        guard_topic=None,
        intent=None,
        slots={},
        tool_calls_made=[],
        tool_results=[],
        rounds=0,
        force_tool=False,
        force_write_tool=None,
        pending_action=None,
        draft="",
        headline="",
        insights=[],
        verify_fail=[],
        verify_passed=False,
        final="",
        answer={},
        citations=[],
        answer_basis="",
    )
