"""定位「你好 → 暂无客户数据可分析」的真实根因。

⚠ 只读：直接跑 graph.run()，不经过 HTTP，不写库。
   目的是打印**中间态**（决策节点原始输出、answer_builder 输入输出），
   而 /api/agent/ask 的响应体看不到这些。

上一版方案（往 meta.py 加 greeting 关键词表）已被实验证伪：
纯 LLM 提示词下寒暄 16/16 全对（含表外说法），
说明模型会答，问题出在**回答被后续环节丢掉或覆盖**。
"""
import sys

sys.path.insert(0, "/app")

from app.agent import answer_builder, graph, meta  # noqa: E402

CASES = ["你好", "谢谢", "吃了没", "在吗", "hello"]

# ── 边界回归：这些**绝不能**走"展示决策节点原答"那条路 ──────────
# 全是需要查数据的问题，必须仍然查库（tool_calls 非空）。
# 若被误判为"非数据类"，模型可能凭记忆编造且无依据 —— 那是安全倒退。
BOUNDARY = [
    "现在高危客户有多少人",         # 直接命中关键词
    "帮我取消已建单待处理状态客户",   # diag116 的原始缺陷问句
    "你好，帮我查一下高危客户有多少人",  # 寒暄 + 数据（复合，必须查）
    "谢谢，那决策阈值是多少",        # 道谢 + 数据（复合，必须查）
    "有哪些工单待处理",
]


def show(q: str):
    print("=" * 70)
    print("问:", q)

    # ① 元问题层是否认得（当前会被判为 None）
    print("  meta.detect() ->", meta.detect(q))

    # ② _needs_data 是否会强制查数据
    print("  _needs_data() ->", graph._needs_data(q))

    # ③ 决策节点：模型实际说了什么
    from app.agent.state import initial_state
    st = initial_state("diag", q)
    try:
        out = graph.node_agent(st)
        msgs = out.get("messages") or []
        if msgs:
            m = msgs[-1]
            tc = getattr(m, "tool_calls", None)
            print("  决策节点 tool_calls ->", tc)
            print("  决策节点 content   ->", repr((m.content or "")[:160]))
    except Exception as e:
        print("  决策节点异常:", type(e).__name__, e)

    # ④ 完整跑一遍，看最终交付给用户的东西
    try:
        r = graph.run(q, session_id="diag")
        ans = r.get("answer") or {}
        print("  最终 basis  ->", r.get("basis"))
        print("  最终 headline ->", repr(ans.get("headline")))
        print("  最终 text   ->", repr((ans.get("text") or "")[:120]))
        print("  最终 warning->", (ans.get("warning") or {}).get("text"))
        print("  tool_calls  ->", [c.get("name") for c in (r.get("tool_calls") or [])])
    except Exception as e:
        print("  graph.run 异常:", type(e).__name__, e)


if __name__ == "__main__":
    print("########## 一、寒暄类（修复目标）##########")
    for q in CASES:
        show(q)

    print("\n\n########## 二、边界回归（绝不能被放松）##########")
    fails = []
    for q in BOUNDARY:
        print("=" * 70)
        print("问:", q)
        print("  _needs_data() ->", graph._needs_data(q))
        try:
            r = graph.run(q, session_id="diag")
            ans = r.get("answer") or {}
            calls = [c.get("name") for c in (r.get("tool_calls") or [])]
            print("  basis ->", r.get("basis"))
            print("  tool_calls ->", calls)
            print("  headline ->", repr((ans.get("headline") or "")[:90]))
            print("  text ->", repr((ans.get("text") or "")[:90]))
            # 断言：数据类问题必须真的查了数据
            if not calls:
                fails.append(f"「{q}」未调用任何工具 —— 边界被放松！")
                print("  [FAIL] 未调用工具")
            if not graph._needs_data(q):
                fails.append(f"「{q}」_needs_data 返回 False —— 闸门失效")
                print("  [FAIL] _needs_data=False")
        except Exception as e:
            fails.append(f"「{q}」异常 {type(e).__name__}: {e}")
            print("  异常:", type(e).__name__, e)

    print("\n" + "=" * 70)
    if fails:
        print(f"边界回归失败 {len(fails)} 项：")
        for f in fails:
            print("  -", f)
    else:
        print(f"边界回归全部通过（{len(BOUNDARY)}/{len(BOUNDARY)}）：数据类问题仍强制查库")
