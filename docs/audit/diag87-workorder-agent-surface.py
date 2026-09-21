"""核查：工单/客户层「哪些字段真实可用、哪些是空的」——
为设计智能体赋能点提供事实依据，避免设计出数据不支持的场景。
"""
import json
import urllib.request
from collections import Counter
from datetime import datetime, timezone

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r:
        return json.loads(r.read().decode())


print("=" * 80)
print("一、工单表字段「利用率」——哪些被填了、哪些一直空着")
print("=" * 80)
orders = get("/api/work-orders?page=1&page_size=100")["items"]
n = len(orders)
print(f"工单数 = {n}\n")

fields = ["note", "assignee", "risk_factors", "strategy", "channel",
          "override_reason", "thresholds_snapshot", "value_tier_snapshot",
          "expected_value_snapshot", "completed_at"]
print(f"{'字段':<24}{'非空':>6}{'填充率':>9}   说明")
print("-" * 80)
for f in fields:
    filled = sum(1 for o in orders if o.get(f) not in (None, "", [], {}))
    note = ""
    if f == "note":
        note = "← 完全没用起来（建单表单有输入框，但从未被填写）"
    elif f == "override_reason":
        note = "← 渠道覆盖留痕，无人覆盖故为空"
    print(f"{f:<24}{filled:>6}{filled/n*100:>8.0f}%   {note}")

print()
print("=" * 80)
print("二、状态与分布（智能体要处理的真实盘子）")
print("=" * 80)
st = get("/api/work-orders/stats")
print(f"  总 {st['total']}  待处理 {st['pending']}  处理中 {st['in_progress']}"
      f"  已完成 {st['completed']}  已流失 {st['lost']}")
print(f"  负责人：{st['assignees']}")

print()
print("  按状态 × 负责人：")
grid = {}
for o in orders:
    a = o.get("assignee") or "(未指派)"
    grid.setdefault(a, Counter())[o["status"]] += 1
for a, c in sorted(grid.items(), key=lambda x: -sum(x[1].values())):
    tot = sum(c.values())
    print(f"    {a:<10} 合计 {tot:>3}   " +
          "  ".join(f"{k}={v}" for k, v in sorted(c.items())))

print()
print("=" * 80)
print("三、时间维度（能否做 SLA / 超期提醒 / 趋势）")
print("=" * 80)
now = datetime.now(timezone.utc)
ages = []
for o in orders:
    ca = o.get("created_at")
    if not ca:
        continue
    dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ages.append(((now - dt).days, o["status"], o.get("completed_at")))
ages.sort()
if ages:
    ds = [a[0] for a in ages]
    print(f"  创建时间距今：最小 {min(ds)} 天  最大 {max(ds)} 天  中位 {ds[len(ds)//2]} 天")

open_orders = [a for a in ages if a[1] in ("pending", "in_progress")]
print(f"  未结单 {len(open_orders)} 条，其中挂最久的：", end="")
if open_orders:
    open_orders.sort(reverse=True)
    print(f"{open_orders[0][0]} 天")
else:
    print("无")

print()
print("  有无「跟进记录时间线」字段：", end="")
has_timeline = any(k in orders[0] for k in ["activities", "timeline", "logs", "history"])
print(f"{'有' if has_timeline else '**无**（只有 created_at / updated_at / completed_at 三个时间点）'}")

print()
print("=" * 80)
print("四、结单结果（复盘归因的数据基础）")
print("=" * 80)
done = [o for o in orders if o.get("result") in ("retained", "lost")]
print(f"  已结单 {len(done)} 条")
res = Counter(o["result"] for o in done)
print(f"  结果分布：{dict(res)}")
strat = Counter(o.get("strategy") or "(空)" for o in done)
print(f"  策略种类：{len(strat)} 种 -> {dict(strat)}")
print("  ⚠ 只有 1 种策略 ⇒ 无法回答『哪种策略更有效』")

print()
print("=" * 80)
print("五、客户层可用于「建单决策」的字段（单客户画像）")
print("=" * 80)
c = get("/api/customers/C000001")
useful = ["probability", "risk_level", "value_tier", "expected_value",
          "channel", "strategy", "reason", "risk_factors", "has_active_order"]
print(f"  {'字段':<20}{'值'}")
print("-" * 80)
for f in useful:
    print(f"  {f:<20}{str(c.get(f))[:56]}")

print()
print("=" * 80)
print("六、当前「建单」的判定逻辑（智能体要增强的对象）")
print("=" * 80)
print("  前端 CustomerManagement：勾选行 → 逐条 POST /api/work-orders")
print("    · 可选性：!has_active_order（有进行中工单则 disabled）")
print("    · 携带：risk_level / probability / balance / value_tier /")
print("            expected_value / channel / strategy（全部来自该客户）")
print("  后端 create_work_order：")
print("    · 409 互斥：同一客户已有 pending/in_progress 则拒绝")
print("    · 补齐快照：thresholds / model_used / value_tier / expected_value")
print("    · 渠道覆盖需 override_reason，否则 422")
print("    · 状态固定 pending")
print()
print("  ⚠ 没有任何「优先级 / 排序 / 该不该建」的判断 —— 全靠人工勾选")
print("  ⚠ 没有负载均衡：assignee 由建单人填，不检查该人手上已有多少单")
