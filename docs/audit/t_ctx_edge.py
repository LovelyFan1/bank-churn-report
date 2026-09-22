"""会话上下文 —— 边界与回归测试（只读，不写库）。

覆盖：
  1. 空上下文（第一轮）仍正常
  2. 显式新编号 → 新话题，不被旧名单干扰
  3. 越界编号（模型幻觉）→ 被输出侧白名单拦住
  4. guard 三类边界问题仍被拦（上下文不影响拦截）
  5. meta 元问题仍走确定性回答
  6. 上下文损坏/恶意输入 → 不崩、不注入
  7. 连续追问不累积 token（context 恒定大小）
"""

import json
import urllib.request

BASE = "http://127.0.0.1:8000/api/agent"


def ask(q, ctx=None):
    body = json.dumps({"question": q, "session_id": "edge",
                       "context": ctx or {}}).encode()
    req = urllib.request.Request(f"{BASE}/ask", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def line(tag, d, extra=""):
    ans = d.get("answer") or {}
    print(f"[{tag}] basis={d.get('basis'):24s} "
          f"head={(ans.get('headline') or '')[:34]:36s} {extra}")


CTX3 = {"customers": [
    {"id": "C071081", "name": "Bentley", "prob": 0.9757, "balance": 225563.4},
    {"id": "C034525", "name": "Goddard", "prob": 0.962, "balance": 219490.05},
    {"id": "C062858", "name": "Pirogov", "prob": 0.8883, "balance": 225534.51},
], "orders": []}

print("=" * 78)
print("① 空上下文（第一轮）—— 不应报错")
d = ask("现在决策阈值是多少？", {})
line("空ctx/阈值", d)

print("\n② 显式新编号 = 新话题 —— 不被旧名单干扰")
d = ask("C034525 这个人要不要打电话？", CTX3)
line("显式ID", d)
pa = d.get("pending_action")
print("     → 查的是 C034525 而非名单里的其他人：",
      [c["args"] for c in d.get("tool_calls", [])][:2])

print("\n③ 越界：直接要求给一个不在名单里的编号建单")
d = ask("给 C099999 建个工单", CTX3)
line("越界建单", d, f"pending={bool(d.get('pending_action'))}")
errs = [c.get("error") for c in d.get("tool_calls", []) if c.get("error")]
print("     拦截记录:", (errs[0][:60] + "…") if errs else "(无)")

print("\n④ guard 三类边界 —— 上下文不得使其失效")
for q in ["挽留成功率实测算出来是多少？",
          "预测一下明年的流失趋势",
          "哪个策略效果更好？"]:
    d = ask(q, CTX3)
    line(f"guard/{q[:8]}", d, f"topic={d.get('guard_topic')}")

print("\n⑤ meta 元问题 —— 仍走确定性回答")
for q in ["你有哪些工具？", "你是什么模型？"]:
    d = ask(q, CTX3)
    line(f"meta/{q[:7]}", d)

print("\n⑥ 恶意/损坏上下文 —— 不崩、不注入")
for bad in [
    {"customers": "DROP TABLE", "orders": None},
    {"customers": [{"id": "'; DROP TABLE x;--"}, {"id": "C071081"}], "orders": [{"order_id": "63"}]},
    {"customers": [None, 123, {"id": "C071081"}], "orders": []},
    [],
    "not-a-dict",
]:
    try:
        d = ask("第一个的余额是多少？", bad)
        line(f"脏ctx/{str(bad)[:16]}", d)
    except Exception as e:
        print(f"[脏ctx/{str(bad)[:16]}] 异常: {type(e).__name__}: {e}")

print("\n⑦ 连续追问：context 体积是否恒定")
ctx = {}
sizes = []
for i, q in enumerate(["挽回价值最高的3个客户", "第二个呢", "第一个呢",
                       "第二个呢", "第一个呢", "第二个呢"], 1):
    d = ask(q, ctx)
    ctx = d.get("context") or {}
    n = len(ctx.get("customers", []))
    sz = len(json.dumps(ctx, ensure_ascii=False))
    sizes.append(sz)
    print(f"     第{i}轮 ctx客户数={n} 字节={sz} basis={d.get('basis')} "
          f"insights={len(d.get('answer',{}).get('insights') or [])}")
print("     → 客户数上限 20，字节数是否收敛：", sizes)

print("\n完成。")
