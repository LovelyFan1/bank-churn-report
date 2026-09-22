"""对话页设计前的事实核查。

要确认四件事，全部实测：
  1) 各端点延迟 —— 决定对话页能否串行调多个
  2) 关键端点的字段结构 —— 决定模板能填哪些槽
  3) sort_by 是否静默回退 —— 这是个"智能体说了但系统没做"的陷阱
  4) 有无任何实时/流式能力 —— 决定对话形态（一次性返回 vs 流式）
"""
import json
import time
import urllib.request

BASE = "http://localhost:8000"


def get(p, t=300):
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + p, timeout=t) as r:
            return r.status, json.loads(r.read().decode()), time.time() - t0
    except Exception as e:
        return None, {"err": f"{type(e).__name__}: {e}"}, time.time() - t0


print("=" * 84)
print("一、延迟（对话页一轮可能调多个）")
print("=" * 84)
eps = [
    "/api/customers/C000001",
    "/api/customers/C000001/suggested-note",
    "/api/customers?page=1&page_size=20",
    "/api/customers/suggested-notes/batch?customer_ids=C000001&customer_ids=C000002",
    "/api/model/risk-info",
    "/api/work-orders/stats",
    "/api/work-orders?page=1&page_size=20",
    "/api/dashboard/summary",
    "/api/cost-benefit/summary",
    "/api/portfolio/matrix",
    "/api/eda/key-insights",
]
lat = []
for p in eps:
    code, d, dt = get(p)
    lat.append((p, code, dt))
    print(f"  {dt:6.2f}s  [{code}]  {p}")
print()
worst = max(l[2] for l in lat)
print(f"  最慢 {worst:.2f}s（{max(lat, key=lambda x: x[2])[0]}）")

print()
print("=" * 84)
print("二、sort_by 是否静默回退（关键）")
print("=" * 84)
for s in ["probability", "expected_value", "balance", "net_profit", "bogus_xyz"]:
    code, d, dt = get(f"/api/customers?page=1&page_size=3&sort_by={s}")
    ids = [c["customer_id"] for c in d["items"]]
    ok = code == 200
    print(f"  sort_by={s:<18} HTTP {code}  首3={ids}")
print()
print("  ⚠ 若非法值与合法值返回同一批人且均为 200 →")
print("    说明后端**静默回退**，调用方无法感知排序未生效。")

print()
print("=" * 84)
print("三、关键端点字段（模板能填的槽）")
print("=" * 84)

_, ri, _ = get("/api/model/risk-info")
print("\n/api/model/risk-info:")
print("   ", sorted(ri.keys()))
print("    thresholds       =", ri.get("thresholds"))
print("    decision_threshold =", ri.get("decision_threshold"))
print("    success_rate     =", ri.get("success_rate"))
print("    cost_ratio       =", ri.get("cost_ratio"))

_, ds, _ = get("/api/dashboard/summary")
print("\n/api/dashboard/summary:")
print("   ", sorted(ds.keys()))
bs = ds.get("business_summary") or {}
print("    business_summary 字段:", sorted(bs.keys()) if bs else "(空)")

_, ws, _ = get("/api/work-orders/stats")
print("\n/api/work-orders/stats:")
print("   ", ws)

_, cb, _ = get("/api/cost-benefit/summary")
print("\n/api/cost-benefit/summary:")
print("   ", sorted(cb.keys()))

_, mx, _ = get("/api/portfolio/matrix")
print("\n/api/portfolio/matrix:")
print("    顶层:", sorted(mx.keys()))
print("    首个 cell:", json.dumps(mx["cells"][0], ensure_ascii=False)[:200]
      if mx.get("cells") else "(无)")

_, ki, _ = get("/api/eda/key-insights")
print("\n/api/eda/key-insights:")
print("   ", json.dumps(ki, ensure_ascii=False)[:300])

print()
print("=" * 84)
print("四、有无流式/实时能力")
print("=" * 84)
with urllib.request.urlopen(BASE + "/openapi.json", timeout=60) as r:
    spec = json.loads(r.read().decode())
ws_paths = [p for p in spec["paths"] if "ws" in p.lower()]
print(f"  WebSocket 端点: {ws_paths or '无'}")
routers = set()
for p in spec["paths"]:
    parts = p.split("/")
    if len(parts) > 2 and parts[1] == "api":
        routers.add(parts[2])
print(f"  路由前缀: {sorted(routers)}")
print(f"  agent 相关: {[p for p in spec['paths'] if 'agent' in p.lower()] or '无'}")
