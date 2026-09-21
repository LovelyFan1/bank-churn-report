"""业务逻辑终检 —— 在重训对齐后，复核全部关键口径与恒等式。

覆盖：
  1) 两套阈值是否各司其职、数值合理
  2) ROI 公式是否含成功率，且可复算
  3) 两个 ROI 是否分离命名
  4) 三接口指标是否一致
  5) 工单快照是否与引擎一致
  6) 成本分项恒等式 benefit - cost_fn - cost_fp == net_profit
  7) 矩阵账目是否对得上
  8) 名单排序是否仍按期望价值（缩量+改阈值后不应退化）
"""
import json
import urllib.request

import numpy as np

BASE = "http://localhost:8000"
fails = []


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


ri = get("/api/model/risk-info")
dm = ri["decision_metrics"]
bs = get("/api/dashboard/summary")["business_summary"]
ds = get("/api/dashboard/summary")
rs = get("/api/cost-benefit/retention-summary")
mt = get("/api/portfolio/matrix")
cb = get("/api/cost-benefit/summary")

print("=" * 80)
print("1) 两套阈值各司其职")
print("=" * 80)
thr = ri["thresholds"]
print(f"    分级线(分位数): critical={thr['critical']}  high={thr['high']}  medium={thr['medium']}")
print(f"    决策线(成本最优): {ri['decision_threshold']}   覆盖率={ri['decision_coverage']*100:.2f}%")
check("分级线递减（critical > high > medium）",
      thr["critical"] > thr["high"] > thr["medium"])
check("决策线与分级线不同语义但都在 (0,1)",
      0 < ri["decision_threshold"] < 1)
d = ds["risk_distribution"]
N = ds["overview"]["total_customers"]
print(f"    分级分布: {d}  合计 {sum(d.values()):,} == {N:,}")
check("分级分布合计 == 客户总数", sum(d.values()) == N)

print()
print("=" * 80)
print("2) ROI 含成功率且可复算")
print("=" * 80)
s = ri["success_rate"]
cr = ri["cost_ratio"]
calc_roi = cr * dm["precision"] * s
print(f"    ROI = {cr} × {dm['precision']} × {s} = {calc_roi:.4f}   接口 = {bs['roi']}")
check("ROI == CR × precision × s", abs(calc_roi - bs["roi"]) < 0.02)
check("s 已对外暴露", s is not None and 0 < s <= 1, f"s={s}")
check("旧口径对照值更低（若不乘 s）", cr * dm["precision"] > bs["roi"])

print()
print("=" * 80)
print("3) 两个 ROI 分离命名")
print("=" * 80)
check("retention 无 roi 字段", "roi" not in rs)
check("retention 有 roi_executed", "roi_executed" in rs, f"= {rs.get('roi_executed')}")
check("retention 有 roi_basis", rs.get("roi_basis") == "executed_orders_only")
check("retention 标为 actual_but_seeded", rs.get("basis") == "actual_but_seeded")

print()
print("=" * 80)
print("4) 三接口指标一致")
print("=" * 80)
print(f"    risk-info    : thr={ri['decision_threshold']} recall={dm['recall']} prec={dm['precision']}")
print(f"    cost-benefit : thr={cb['optimal_threshold']} recall={cb['model_recall']} prec={cb['model_precision']}")
print(f"    dashboard    : thr={bs['optimal_threshold']} recall={bs['model_recall']} prec={bs['model_precision']}")
check("三接口阈值一致",
      ri["decision_threshold"] == cb["optimal_threshold"] == bs["optimal_threshold"])
check("三接口 recall 一致",
      dm["recall"] == cb["model_recall"] == bs["model_recall"])
check("三接口 precision 一致",
      dm["precision"] == cb["model_precision"] == bs["model_precision"])

print()
print("=" * 80)
print("5) 工单快照与引擎一致")
print("=" * 80)
wo = get("/api/work-orders?page=1&page_size=5")
snaps = [w.get("thresholds_snapshot") for w in wo["items"]]
same = all(
    sn and abs(sn["critical"] - thr["critical"]) < 1e-9
    and abs(sn["high"] - thr["high"]) < 1e-9
    and abs(sn["medium"] - thr["medium"]) < 1e-9
    for sn in snaps
)
print(f"    工单快照样例: {snaps[0]}")
print(f"    引擎分级线  : {thr}")
check("工单快照 == 当前引擎分级线", same)
check("工单 model_used 与引擎一致",
      all(w.get("model_used") == ri["model"] for w in wo["items"]),
      f"引擎={ri['model']}")

print()
print("=" * 80)
print("6) 成本分项恒等式")
print("=" * 80)
cbth = get("/api/cost-benefit/thresholds")
om = cbth["optimal_metrics"]
lhs = om["benefit"] - om["cost_fn"] - om["cost_fp"]
print(f"    benefit({om['benefit']}) - cost_fn({om['cost_fn']}) - cost_fp({om['cost_fp']})")
print(f"      = {lhs}     net_profit = {om['net_profit']}")
check("benefit - cost_fn - cost_fp == net_profit", abs(lhs - om["net_profit"]) < 1e-6)
# 逐档验算
bad = []
for r in cbth["thresholds"]:
    v = r["benefit"] - r["cost_fn"] - r["cost_fp"]
    if abs(v - r["net_profit"]) > 1e-6:
        bad.append(r["threshold"])
print(f"    19 档逐一验算，不一致档位: {bad or '无'}")
check("全部档位恒等式成立", not bad)

print()
print("=" * 80)
print("7) 矩阵账目")
print("=" * 80)
cells = mt["cells"]
tot = sum(c["count"] for c in cells)
print(f"    cells 合计 {tot:,} == test_size {mt['test_size']:,}")
check("矩阵人数合计 == 测试集规模", tot == mt["test_size"])
check("tier_totals 合计 == 测试集",
      sum(t["count"] for t in mt["tier_totals"]) == mt["test_size"])
check("level_totals 合计 == 测试集",
      sum(t["count"] for t in mt["level_totals"]) == mt["test_size"])

print()
print("=" * 80)
print("8) 名单排序")
print("=" * 80)
ev = get("/api/customers?page=1&page_size=100&sort_by=expected_value&sort_order=desc")
pb_ = get("/api/customers?page=1&page_size=100&sort_by=probability&sort_order=desc")
ev_ids = {x["customer_id"] for x in ev["items"]}
p_ids = {x["customer_id"] for x in pb_["items"]}
inter = len(ev_ids & p_ids)
ev_sum = sum(x["expected_value"] for x in ev["items"])
p_sum = sum(x["expected_value"] for x in pb_["items"])
print(f"    Top100 重合 {inter}/100")
print(f"    期望价值合计: EV排序 {ev_sum:,.0f} vs 概率排序 {p_sum:,.0f}")
check("EV 排序优于概率排序", ev_sum > p_sum,
      f"多 {((ev_sum/p_sum-1)*100):+.1f}%")

print()
print("=" * 80)
print(f"结果: {'全部通过' if not fails else '未通过 → ' + ', '.join(fails)}")
print("=" * 80)
