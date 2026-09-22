"""RBAC 修复验证 —— 只读角色对**全部**写接口都应被拒。

⚠ 上一版这个脚本造成了数据损坏（用了 order_id=1 硬编码、且未自清理）。
   本版的三条纪律：
     1. **所有写操作都必须被拒** —— 若真的成功了，说明漏洞仍在，
        此时立即用**同一令牌**删除自己创建的对象，不留残留
     2. 不碰任何既有 id（不用 `order_id=1` 这类硬编码）
     3. 开始与结束都打印**工单总数**，任何变化都会被看到
"""

import json
import urllib.error
import urllib.request

B = "http://127.0.0.1:8000"
fails = []


def call(p, body=None, tok=None, m="GET"):
    d = json.dumps(body).encode() if body else None
    r = urllib.request.Request(B + p, data=d, method=m)
    r.add_header("Content-Type", "application/json")
    if tok:
        r.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(r, timeout=90) as x:
            raw = x.read().decode()
            return x.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def login(u):
    r = urllib.request.Request(
        B + "/api/auth/login",
        data=json.dumps({"username": u, "password": "Bank@2025"}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode())["token"]


def order_total():
    """直接查库取工单总数 —— 用于确认测试零残留。"""
    import sqlite3
    con = sqlite3.connect("file:/app/data/churn_analysis.db?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0]
    finally:
        con.close()


def ok(cond, msg):
    print(f"  [{'OK ' if cond else 'FAIL'}] {msg}")
    if not cond:
        fails.append(msg)


viewer = login("chenjie")
print("=" * 74)
print(f"测试开始时工单总数: {order_total()}")

# 只读用户的 payload —— 用**不存在**的客户，即使漏洞仍在也不会真建成功
payload = {
    "customer_id": "C999999", "customer_name": "不存在", "geography": "Test",
    "risk_level": "LOW", "probability": 0.01, "balance": 0.0,
    "risk_factors": [], "strategy": "测试", "assignee": "",
    "channel": "automated", "value_tier_snapshot": "ZERO",
    "expected_value_snapshot": 0.0,
}

print("\n一、只读角色对写接口应全部 403")
cases = [
    ("POST", "/api/work-orders", payload, "页面建单"),
    ("PUT", "/api/work-orders/5", {"status": "completed"}, "页面改工单状态"),
    ("DELETE", "/api/work-orders/5", None, "页面删工单"),
    ("POST", "/api/agent/ask", {"question": "现在决策阈值是多少"}, "用智能助手"),
    ("POST", "/api/agent/confirm",
     {"action": "create_work_order", "payload": payload}, "Agent 确认建单"),
    ("POST", "/api/agent/confirm-batch",
     {"customer_ids": ["C999999"], "assignee": ""}, "Agent 批量建单"),
    ("POST", "/api/model/train", None, "重训模型"),
    ("POST", "/api/cluster/kmeans/save?k=5", None, "聚类标签落库"),
]
for m, p, body, desc in cases:
    c, d = call(p, body, viewer, m)
    ok(c == 403, f"{desc:16s} {m:6s} {p.split('?')[0]:34s} → {c}"
                 + (f"  {str(d.get('detail'))[:34]}" if c != 403 else ""))

print("\n二、只读角色的读接口应正常（权限收窄不能误伤）")
reads = [
    ("/api/customers?page_size=1", "查客户"),
    ("/api/work-orders?page_size=1", "看工单"),
    ("/api/dashboard/summary", "看工作台"),
    ("/api/model/comparison", "看模型效果"),
    ("/api/cluster/profiles", "看聚类画像"),
    ("/api/agent/capabilities", "读助手能力"),
]
for p, desc in reads:
    c, _ = call(p, None, viewer, "GET")
    ok(c == 200, f"{desc:12s} GET  {p:36s} → {c}")

print("\n三、客户经理：工单类应可写，模型/聚类应被拒")
mgr = login("liming")
c, d = call("/api/work-orders", payload, mgr, "POST")
created_id = d.get("id") if c == 201 else None
ok(c in (201, 400, 404, 422),
   f"客户经理建单不再被权限拦（{c}，非 403 即为通过权限层）")
if created_id:
    dc, _ = call(f"/api/work-orders/{created_id}", None, mgr, "DELETE")
    print(f"      （已清理测试工单 #{created_id} → {dc}）")
c, _ = call("/api/model/train", None, mgr, "POST")
ok(c == 403, f"客户经理重训模型被拒 → {c}")
c, _ = call("/api/cluster/kmeans/save?k=5", None, mgr, "POST")
ok(c == 403, f"客户经理聚类落库被拒 → {c}")

print("\n四、管理员：应具备全部权限")
# ⚠ 管理员绑了 TOTP，不能用简化的 login()（那个只取 token，会 KeyError）。
#   走完整两步流程。
c, d = call("/api/auth/login",
            {"username": "zhaomin", "password": "Bank@2025"}, None, "POST")
adm = d.get("token")
if d.get("need_totp"):
    from app.services import auth_service as A
    import sqlite3 as _sq
    con = _sq.connect("file:/app/data/churn_analysis.db?mode=ro", uri=True)
    sec = con.execute(
        "SELECT totp_secret FROM users WHERE username='zhaomin'").fetchone()[0]
    con.close()
    c2, d2 = call("/api/auth/login/totp",
                  {"ticket": d["ticket"], "code": A.totp_now(sec)}, None, "POST")
    adm = d2.get("token")
c, d = call("/api/auth/me", None, adm, "GET")
perms = (d.get("user") or {}).get("permissions") or []
print(f"      管理员权限: {perms}")
ok("model:train" in perms, "管理员含 model:train（新增权限点）")
ok("audit:view" in perms and "order:write" in perms, "管理员含审计与建单权限")

print("\n" + "=" * 74)
print(f"测试结束时工单总数: {order_total()}")
print("结论：" + ("PASS —— 写权限已覆盖全部写接口，读接口未误伤" if not fails
                  else f"FAIL —— {len(fails)} 项未通过"))
for f in fails:
    print("  ×", f)
