"""讨论前的事实核查：批量建单的三个真实约束。

用户的三个想法里，"为这三人建单"的按钮最需要事实支撑：
 1) 这三人现在有没有进行中的工单？（后端 409 互斥）
 2) 他们各自值不值得救？（个体经济性判定）
 3) 批量建单会部分失败吗？失败原因是什么？
 4) session_id 目前在服务端有没有被存储？（决定"联想"要靠什么实现）
"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=60) as r:
        return json.loads(r.read().decode())


def post(p, body):
    req = urllib.request.Request(
        BASE + p, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:200]}


print("=" * 86)
print("一、挽回价值最高的 3 人：现状")
print("=" * 86)
d = get("/api/customers?page=1&page_size=3&sort_by=expected_value&sort_order=desc")
top3 = d["items"]
for c in top3:
    cid = c["customer_id"]
    note = get(f"/api/customers/{cid}/suggested-note")
    w = note["worthiness"]
    print(f"\n  {cid}  {c['surname']}")
    print(f"    概率 {c['probability']}   余额 ¥{c['balance']:,.0f}   "
          f"期望价值 ¥{c['expected_value']:,.0f}")
    print(f"    等级 {c['risk_level']}   价值层 {c['value_tier']}")
    print(f"    有进行中工单: {c['has_active_order']}")
    print(f"    经济性判定: {w['verdict']}  净收益 ¥{w['net']:,.0f}  "
          f"（平衡点 ¥{w['breakeven']:,.0f}，比值 {w['ratio']}）")

print()
print("=" * 86)
print("二、批量建单会怎样（用真实写入测试，测后立即删除）")
print("=" * 86)
before = get("/api/work-orders/stats")["total"]
print(f"  起始工单数 {before}")

created = []
try:
    for c in top3:
        # 复用 Agent 的 propose → confirm 路径，模拟前端逐条确认
        code, r = post("/api/agent/ask",
                       {"question": f"给 {c['customer_id']} 建单"})
        pa = r.get("pending_action")
        if not pa:
            print(f"  {c['customer_id']}: 未生成待确认动作（basis={r.get('basis')}）")
            continue
        code, res = post("/api/agent/confirm",
                         {"action": pa["action"], "payload": pa["payload"]})
        if code == 200:
            oid = (res.get("order") or {}).get("id")
            created.append(oid)
            print(f"  {c['customer_id']}: 建单成功 #{oid}")
        else:
            print(f"  {c['customer_id']}: HTTP {code} → "
                  f"{json.dumps(res, ensure_ascii=False)[:120]}")

    mid = get("/api/work-orders/stats")["total"]
    print(f"\n  建单后工单数 {mid}（+{mid - before}）")

    print()
    print("  再建一次同一批（验证 409 互斥的表现）：")
    for c in top3:
        code, r = post("/api/agent/ask",
                       {"question": f"给 {c['customer_id']} 建单"})
        pa = r.get("pending_action")
        code2, res = post("/api/agent/confirm",
                          {"action": pa["action"], "payload": pa["payload"]})
        msg = json.dumps(res, ensure_ascii=False)
        print(f"    {c['customer_id']}: HTTP {code2} → {msg[:110]}")

finally:
    print()
    print("  清理：")
    for oid in created:
        req = urllib.request.Request(f"{BASE}/api/work-orders/{oid}", method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=60)
            print(f"    删除 #{oid}")
        except Exception as e:
            print(f"    删除 #{oid} 失败 {e}")
    after = get("/api/work-orders/stats")["total"]
    print(f"  最终工单数 {after}（应复原为 {before}）")

print()
print("=" * 86)
print("三、会话状态：session_id 在服务端是否被存储")
print("=" * 86)
import subprocess
r = subprocess.run(
    ["grep", "-rn", "session_id", "/app/app/agent/", "/app/app/routers/agent.py"],
    capture_output=True, text=True)
print(r.stdout or "(无匹配)")
print("  → 若仅出现在入参与回显、无任何存储/读取，则服务端**无会话记忆**")
