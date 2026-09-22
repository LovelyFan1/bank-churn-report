"""验证：重复工具调用去重（构造性测试，不靠运气）。

背景（实测 diag126）：问「你有哪些工具」时，模型在**同一个 AIMessage 里**
重复发起了相同的工具调用 —— 3 个工具各调 4 遍，共 12 次。

⚠ 为什么要构造性测试：
   "模型是否会重复调用"取决于模型当时的行为，**不稳定**。
   只跑真实提问可能一次都碰不到重复，于是测试"通过"但其实没验证到东西。
   故本脚本直接**构造**一个含重复 tool_calls 的 AIMessage 喂给 node_tools，
   确定性地检验去重逻辑。

去重规则（当前实现）：
  · 同**一轮**内 (工具名, 参数) 完全相同 → 只执行一次
  · 跨轮不去重（模型可能确实需要重查）
"""
import json
import sys

sys.path.insert(0, "/app")

from langchain_core.messages import AIMessage  # noqa: E402

from app.agent import graph as G  # noqa: E402
from app.agent.state import initial_state  # noqa: E402

fails = []
out = []


def p(s=""):
    out.append(str(s))


class _Counter:
    """记录 call_tool 被真实调用的次数。"""
    def __init__(self):
        self.calls = []


counter = _Counter()
_orig_call = G.tools.call_tool


def _spy(name, args):
    counter.calls.append((name, json.dumps(args, sort_keys=True, ensure_ascii=False)))
    return _orig_call(name, args)


G.tools.call_tool = _spy

try:
    # ── 用例 1：同一轮内重复 4 次相同调用 ─────────────────
    p("=" * 88)
    p("一、构造：同一轮内 3 个工具各重复 4 次（复现 diag126 的现场）")
    p("=" * 88)
    names = ["get_model_thresholds", "get_business_summary", "get_workorder_stats"]
    tool_calls = []
    for rep in range(4):
        for nm in names:
            tool_calls.append({"name": nm, "args": {}, "id": f"c_{nm}_{rep}"})

    st = initial_state("t", "你有哪些工具")
    st["messages"] = [AIMessage(content="", tool_calls=tool_calls)]

    counter.calls.clear()
    res = G.node_tools(st)

    p(f"  构造的 tool_calls 数: {len(tool_calls)}（{len(names)} 个工具 × 4 次）")
    p(f"  实际执行的调用数:     {len(counter.calls)}")
    p(f"  tool_calls_made 长度: {len(res['tool_calls_made'])}")
    p(f"  tool_results 长度:    {len(res['tool_results'])}")
    p(f"  执行明细: {[c[0] for c in counter.calls]}")

    if len(counter.calls) != len(names):
        fails.append(f"去重失效：应执行 {len(names)} 次，实执行 {len(counter.calls)} 次")
    p(f"  [{'OK ' if len(counter.calls) == len(names) else 'FAIL'}] "
      f"重复调用被去重（{len(tool_calls)} → {len(counter.calls)}）")

    # 去重后每个工具只应有一条结果，且顺序与首次出现一致
    got = [r["tool"] for r in res["tool_results"]]
    if got != names:
        fails.append(f"去重后工具顺序/集合不符：{got} != {names}")
    p(f"  [{'OK ' if got == names else 'FAIL'}] 结果集正确：{got}")

    # ── 用例 2：参数不同的同名调用**不得**被去重 ──────────
    p()
    p("=" * 88)
    p("二、参数不同的同名调用必须都执行（不能过度去重）")
    p("=" * 88)
    tc2 = [
        {"name": "get_customer_risk", "args": {"customer_id": "C034525"}, "id": "a"},
        {"name": "get_customer_risk", "args": {"customer_id": "C062858"}, "id": "b"},
        {"name": "get_customer_risk", "args": {"customer_id": "C034525"}, "id": "c"},
    ]
    st2 = initial_state("t", "查两个客户")
    st2["messages"] = [AIMessage(content="", tool_calls=tc2)]
    counter.calls.clear()
    res2 = G.node_tools(st2)
    p(f"  构造 3 次调用（2 个不同客户 + 1 次重复）")
    p(f"  实际执行: {len(counter.calls)} 次 → {[c[0] for c in counter.calls]}")
    if len(counter.calls) != 2:
        fails.append(f"参数不同的同名调用被误去重：执行 {len(counter.calls)} 次，应为 2")
    p(f"  [{'OK ' if len(counter.calls) == 2 else 'FAIL'}] 只去掉了完全相同的那个")

    # ── 用例 3：跨轮不去重 ────────────────────────────────
    p()
    p("=" * 88)
    p("三、跨轮不去重（模型可能确实需要重查）")
    p("=" * 88)
    st3 = initial_state("t", "再查一次")
    st3["messages"] = [AIMessage(content="", tool_calls=[
        {"name": "get_model_thresholds", "args": {}, "id": "x"}])]
    counter.calls.clear()
    r3a = G.node_tools(st3)
    # 第二轮：同一工具同参数
    st3["messages"] = st3["messages"] + [AIMessage(content="", tool_calls=[
        {"name": "get_model_thresholds", "args": {}, "id": "y"}])]
    st3["tool_calls_made"] = r3a["tool_calls_made"]
    st3["tool_results"] = r3a["tool_results"]
    r3b = G.node_tools(st3)
    p(f"  第一轮执行 {len(r3a['tool_calls_made'])} 次，第二轮再执行 "
      f"{len(r3b['tool_calls_made']) - len(r3a['tool_calls_made'])} 次")
    if len(r3b["tool_calls_made"]) != 2:
        fails.append("跨轮被错误去重（第二轮应重新执行）")
    p(f"  [{'OK ' if len(r3b['tool_calls_made']) == 2 else 'FAIL'}] 跨轮重查正常")
finally:
    G.tools.call_tool = _orig_call

p()
p("=" * 88)
if fails:
    p(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        p(f"  · {f}")
else:
    p("结论：PASS —— 轮内去重生效、参数不同不误去重、跨轮允许重查")
p("=" * 88)

with open("/tmp/diag129.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("written")
