"""诊断：为什么写操作没有转为待确认动作。

现象（diag107）：问「帮我给 C034525 建个挽留工单，负责人写张思远」
  → basis=llm_verified, pending_action=无, 工单数未变
即：**没有写库**（好），但**也没有生成待确认动作**（功能缺失）。

需要确认：LLM 到底调了哪个工具？是没调、调错了、还是调对了但没被识别？
"""
import json
import urllib.request

BASE = "http://localhost:8000"


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


print("=" * 88)
print("复现：直接问建单")
print("=" * 88)
r = post("/api/agent/ask", {"question": "帮我给 C034525 建个挽留工单，负责人写张思远"})
print(f"  basis          = {r['basis']}")
print(f"  tool_calls     = {json.dumps(r['tool_calls'], ensure_ascii=False)}")
print(f"  pending_action = {r['pending_action']}")
print(f"  verify_failed  = {r['verify_failed']}")
print()
print("  回答全文：")
print("  " + (r["answer"] or "").replace("\n", "\n  "))

print()
print("=" * 88)
print("对照：换几种说法，看是否稳定复现")
print("=" * 88)
for q in [
    "给 C034525 建单",
    "把 C034525 派给张思远",
    "为 C034525 创建工单",
    "C034525 需要建单跟进",
]:
    try:
        r = post("/api/agent/ask", {"question": q})
        calls = [c["name"] for c in r["tool_calls"]]
        print(f"\n  {q}")
        print(f"    调用的工具 = {calls}")
        print(f"    pending    = {'有' if r['pending_action'] else '无'}")
    except Exception as e:
        print(f"\n  {q}\n    [失败] {type(e).__name__}: {e}")

print()
print("=" * 88)
print("直接测工具本身（排除工具实现问题）")
print("=" * 88)
r = post("/api/agent/ask", {"question": "不要解释，直接调用 propose_create_work_order 工具，customer_id=C034525"})
print(f"  tool_calls = {json.dumps(r['tool_calls'], ensure_ascii=False)}")
print(f"  pending    = {'有' if r['pending_action'] else '无'}")
if r["pending_action"]:
    print(f"    summary = {r['pending_action'].get('summary')}")
