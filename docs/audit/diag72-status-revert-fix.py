"""验证后端「回退清痕」修复（用临时工单，测完删除）。

修复前实测：
    completed → in_progress  后 result 仍为 retained、completed_at 仍在
修复后应：
    completed → in_progress  时 result 与 completed_at 都被清空
且不能破坏原有行为：结案时仍自动补 result、显式传 result 仍优先。

⚠ 记录创建的工单 id，结束时删除，保证工单总数恢复。
"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
CID = "C000002"


def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            b = r.read().decode()
            return r.status, (json.loads(b) if b else None)
    except urllib.error.HTTPError as e:
        b = e.read().decode()
        try:
            return e.code, json.loads(b)
        except Exception:
            return e.code, b


created = []
fails = []


def expect(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


# 清理上次残留
code, lst = call("/api/work-orders?page=1&page_size=100")
for o in (lst or {}).get("items", []):
    if o.get("customer_name") == "PROBE2":
        call(f"/api/work-orders/{o['id']}", "DELETE")

code, before = call("/api/work-orders/stats")
print(f"基线工单数 = {before['total']}\n")

try:
    code, o = call("/api/work-orders", "POST", {
        "customer_id": CID, "customer_name": "PROBE2", "geography": "France",
        "risk_level": "HIGH", "probability": 0.5, "balance": 100000.0,
        "risk_factors": ["t"], "assignee": "测试",
    })
    assert code == 201, (code, o)
    created.append(o["id"])
    oid = o["id"]

    print("场景 1：结案自动补 result（原有行为，不能破坏）")
    code, o = call(f"/api/work-orders/{oid}", "PUT", {"status": "completed"})
    expect("completed → result=retained", o["result"] == "retained", f"实际 {o['result']}")
    expect("completed → completed_at 有值", o["completed_at"] is not None)

    print("\n场景 2：★ 回退清痕（本次修复的核心）")
    code, o = call(f"/api/work-orders/{oid}", "PUT", {"status": "in_progress"})
    expect("in_progress → result 被清空", o["result"] is None, f"实际 {o['result']!r}")
    expect("in_progress → completed_at 被清空", o["completed_at"] is None,
           f"实际 {o['completed_at']!r}")

    print("\n场景 3：lost 结案后回退到 pending")
    call(f"/api/work-orders/{oid}", "PUT", {"status": "lost"})
    code, o = call(f"/api/work-orders/{oid}", "PUT", {"status": "pending"})
    expect("lost → pending 后 result 清空", o["result"] is None, f"实际 {o['result']!r}")
    expect("lost → pending 后 completed_at 清空", o["completed_at"] is None)

    print("\n场景 4：显式传 result 仍优先（不能破坏）")
    code, o = call(f"/api/work-orders/{oid}", "PUT",
                   {"status": "completed", "result": "lost"})
    expect("显式 result=lost 被尊重", o["result"] == "lost", f"实际 {o['result']}")

    print("\n场景 5：completed → lost（结案态之间切换）")
    code, o = call(f"/api/work-orders/{oid}", "PUT", {"status": "lost"})
    expect("completed → lost：result 变 lost", o["result"] == "lost", f"实际 {o['result']}")
    expect("completed → lost：completed_at 更新为有值", o["completed_at"] is not None)

    print("\n场景 6：终态可回退（后端本允许，验证未被误伤）")
    for target in ["pending", "in_progress"]:
        code, o = call(f"/api/work-orders/{oid}", "PUT", {"status": target})
        expect(f"lost → {target} 返回 200", code == 200, f"HTTP {code}")
        expect(f"lost → {target} 无结案残留",
               o["result"] is None and o["completed_at"] is None)

    print("\n场景 7：非结案态改其他字段，不应误清 result")
    call(f"/api/work-orders/{oid}", "PUT", {"status": "completed"})
    code, o = call(f"/api/work-orders/{oid}", "PUT", {"note": "只改备注"})
    expect("只改 note 不动 status → result 保留", o["result"] == "retained",
           f"实际 {o['result']!r}")
    expect("只改 note 不动 status → completed_at 保留", o["completed_at"] is not None)

finally:
    for i in created:
        call(f"/api/work-orders/{i}", "DELETE")
    print(f"\n[清理] 删除测试工单 {created}")

code, after = call("/api/work-orders/stats")
print(f"清理后工单数 = {after['total']}（基线 {before['total']}）")
print("恢复一致" if after["total"] == before["total"] else "!! 数量不一致")

print()
print("=" * 60)
print(f"结果: {'全部通过' if not fails else '未通过 — ' + ', '.join(fails)}")
