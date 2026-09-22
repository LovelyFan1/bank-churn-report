"""会话上下文（追问）实测 —— 直接打后端接口，走真实 LLM。

⚠ 这是**只读**测试：不调用 /confirm，不产生写操作。
   建单类追问只验证它产出 pending_action，绝不点确认。

用法（容器内）：python /tmp/t_ctx.py
"""

import json
import urllib.request

BASE = "http://127.0.0.1:8000/api/agent"


def ask(q, ctx):
    body = json.dumps({"question": q, "session_id": "ctxtest",
                       "context": ctx}).encode()
    req = urllib.request.Request(f"{BASE}/ask", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def show(tag, d):
    ans = d.get("answer") or {}
    ent = ans.get("entities") or []
    ids = [e.get("customer_id") or e.get("order_id") for e in ent]
    print(f"\n===== {tag} =====")
    print("basis   :", d.get("basis"))
    print("headline:", ans.get("headline"))
    print("entities:", ids)
    print("insights:", ans.get("insights"))
    print("tools   :", [(c["name"], c["ok"]) for c in d.get("tool_calls", [])])
    if d.get("verify_failed"):
        print("ORPHANS :", d["verify_failed"])
    pa = d.get("pending_action")
    if pa:
        print("pending :", pa.get("action"),
              (pa.get("payload") or {}).get("customer_id"))
    print("context :", json.dumps(d.get("context"), ensure_ascii=False))


ctx = {}
steps = [
    "帮我调出挽回价值最高的3个客户",
    "第二个为什么值得投入",
    "他的余额是多少",
    "给他建个工单",
]
for i, q in enumerate(steps, 1):
    d = ask(q, ctx)
    show(f"第{i}轮 {q}", d)
    ctx = d.get("context") or {}
    print(">>> ctx customers:", [c["id"] for c in ctx.get("customers", [])])
