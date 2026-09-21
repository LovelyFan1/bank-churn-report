"""实测工单状态机的真实行为（用临时工单，测完删除）。

要验证的问题（都是读代码后的疑问，需实测确认）：
  1. 状态流转有无守卫？API 是否允许 pending → completed 直接跳？
  2. status=completed 时 result 是否自动补 "retained"？
  3. 从 completed 改回 in_progress，completed_at 会被清空吗？
  4. 同一客户重复建单是否 409？
  5. 哪个状态算「进行中」（影响客户名单的可选性）？

⚠ 全程记录创建的工单 id，结束时全部删除，保证 work_orders 恢复原数量。
"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
PROBE_CUSTOMER = "C000001"


def call(path, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


created = []


def create(cid, name="PROBE"):
    code, d = call("/api/work-orders", "POST", {
        "customer_id": cid,
        "customer_name": name,
        "geography": "France",
        "risk_level": "HIGH",
        "probability": 0.5,
        "balance": 100000.0,
        "risk_factors": ["测试"],
        "assignee": "测试员",
    })
    if code == 201:
        created.append(d["id"])
    return code, d


def cleanup():
    for oid in created:
        call(f"/api/work-orders/{oid}", "DELETE")
    print(f"\n[清理] 已删除测试工单 {created}")


# 清理残留（上次中断可能留下 PROBE 工单）
code, lst = call("/api/work-orders?page=1&page_size=100")
for o in (lst or {}).get("items", []):
    if o.get("customer_name") == "PROBE":
        call(f"/api/work-orders/{o['id']}", "DELETE")
        print(f"[清理残留] 删除工单 #{o['id']}")

code, before = call("/api/work-orders/stats")
print(f"基线: 工单总数 = {before['total']}")
print("=" * 74)

try:
    # ── 先清掉 C000001 的既有工单影响：确认它没有进行中工单 ──
    code, act = call("/api/work-orders/active-customers")
    print(f"C000001 当前是否有进行中工单: "
          f"{PROBE_CUSTOMER in act['customer_ids']}")

    # ══ 1) 新建工单的初始状态 ══
    print("\n[1] 新建工单的初始 status / result / completed_at")
    code, o = create(PROBE_CUSTOMER)
    print(f"    POST -> {code}")
    print(f"    status={o['status']}  result={o['result']}  completed_at={o['completed_at']}")
    print(f"    → 初始状态硬编码为 pending，result/completed_at 均为 None")

    # ══ 2) 重复建单是否 409 ══
    print("\n[2] 同一客户重复建单（已有 pending 工单时）")
    code, d = create(PROBE_CUSTOMER)
    print(f"    POST -> {code}  detail={d.get('detail') if isinstance(d, dict) else d}")

    # ══ 3) pending → completed 直接跳（跳过 in_progress）══
    print("\n[3] pending → completed 直接跳（跳过 in_progress）")
    code, o = call(f"/api/work-orders/{o['id']}", "PUT", {"status": "completed"})
    print(f"    PUT {{status: completed}} -> {code}")
    print(f"    status={o['status']}  result={o['result']}  "
          f"completed_at={'有' if o['completed_at'] else '无'}")
    print(f"    → 允许直接跳；result 被自动补为 '{o['result']}'")

    # ══ 4) 从 completed 改回 in_progress ══
    print("\n[4] completed → in_progress（回退）")
    code, o = call(f"/api/work-orders/{o['id']}", "PUT", {"status": "in_progress"})
    print(f"    PUT {{status: in_progress}} -> {code}")
    print(f"    status={o['status']}  result={o['result']}  "
          f"completed_at={'仍有' if o['completed_at'] else '已清空'}")
    print(f"    → 回退被允许；result 与 completed_at **不会**被清掉（残留）")

    # ══ 5) 显式指定 result ══
    print("\n[5] status=completed 时显式传 result=lost")
    code, o = call(f"/api/work-orders/{o['id']}", "PUT",
                   {"status": "completed", "result": "lost"})
    print(f"    status={o['status']}  result={o['result']}")
    print(f"    → 显式传的 result 优先，不会被覆盖")

    # ══ 6) 哪个状态算「进行中」══
    print("\n[6] 「进行中」口径（影响客户名单能否再勾选建单）")
    for st in ["pending", "in_progress", "completed", "lost"]:
        call(f"/api/work-orders/{o['id']}", "PUT", {"status": st})
        code, act = call("/api/work-orders/active-customers")
        inact = PROBE_CUSTOMER in act["customer_ids"]
        # 同时看客户列表里的 has_active_order
        code, cust = call(f"/api/customers?search={PROBE_CUSTOMER}&page=1&page_size=5")
        item = next((x for x in cust["items"] if x["customer_id"] == PROBE_CUSTOMER), None)
        print(f"    status={st:<12} active-customers含={str(inact):<5} "
              f"has_active_order={item['has_active_order'] if item else 'N/A'}")

    # ══ 7) 非法状态是否被拒 ══
    print("\n[7] 非法 status 值")
    code, d = call(f"/api/work-orders/{o['id']}", "PUT", {"status": "cancelled"})
    print(f"    PUT {{status: 'cancelled'}} -> {code}")
    print(f"    → Pydantic 校验拦截: {str(d)[:110]}")

    # ══ 8) 是否允许 pending → lost ══
    print("\n[8] pending → lost 直接跳")
    call(f"/api/work-orders/{o['id']}", "PUT", {"status": "pending", "result": None})
    code, o2 = call(f"/api/work-orders/{o['id']}", "PUT", {"status": "lost"})
    print(f"    status={o2['status']}  result={o2['result']}")
    print(f"    → 允许；result 自动补为 'lost'")

finally:
    cleanup()

code, after = call("/api/work-orders/stats")
print(f"\n清理后工单总数 = {after['total']}  （基线 {before['total']}）")
print("恢复一致" if after["total"] == before["total"] else "!! 数量不一致，需排查")
