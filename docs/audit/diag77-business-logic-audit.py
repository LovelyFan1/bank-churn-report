"""业务逻辑口径审计 —— 实测每个对外数字的推导链。

要回答的问题：
  1. 分级阈值（分位数）与决策阈值（成本最优）各覆盖多少人？两者差多少？
  2. 仪表盘的"高风险客户"到底指哪个口径？与"会真正被干预的人"是否一致？
  3. 成本收益的"年度"口径是否成立（数据有时间维度吗）？
  4. ROI 的假设链：是否隐含"挽留成功率 100%"？
  5. 模型推算的挽留效果 vs 工单实测的挽留效果，差多少？
"""
import json
import urllib.request

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=240) as r:
        return json.loads(r.read().decode("utf-8"))


print("=" * 82)
print("一、两套阈值体系：各覆盖多少人")
print("=" * 82)

dash = get("/api/dashboard/summary")
ov = dash["overview"]
risk_dist = dash["risk_distribution"]
N = ov["total_customers"]

print(f"客户总数 = {N:,}")
print()
print("【A】分级阈值（原始概率分位数 P95/P70/P35）→ 用于贴标签")
tot_a = 0
for k in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
    v = risk_dist.get(k, 0)
    tot_a += v
    print(f"    {k:<9} {v:>7,}  {v/N*100:5.1f}%")
print(f"    {'合计':<9} {tot_a:>7,}")
print("    → 占比恒定（5%/25%/35%/35%），与业务量无关")

ri = get("/api/model/risk-info")
thr = ri["thresholds"]
dm = ri["decision_metrics"]
dt = ri["decision_threshold"]
cov = ri["decision_coverage"]

print()
print("【B】决策阈值（训练集净收益最优的绝对切点）→ 用于决定是否真的干预")
print(f"    阈值 = {dt}")
print(f"    覆盖率 = {cov*100:.2f}%  → 约 {int(N*cov):,} 人会进名单")
print(f"    分级阈值 = critical {thr['critical']:.4f} / high {thr['high']:.4f} / medium {thr['medium']:.4f}")

print()
print("【关键差异】")
crit_n = risk_dist.get("CRITICAL", 0)
flag_n = int(N * cov)
print(f"    界面标为 CRITICAL 的        : {crit_n:>7,}  ({crit_n/N*100:.1f}%)")
print(f"    按决策阈值会真正被干预的    : {flag_n:>7,}  ({cov*100:.1f}%)")
print(f"    倍数                        : {flag_n/crit_n:.1f}x")
print("    → 同一系统两个'高风险'口径，相差 7 倍")

print()
print("=" * 82)
print("二、成本收益的数字推导链")
print("=" * 82)
bs = dash["business_summary"]
for k, v in bs.items():
    if isinstance(v, (int, float)) and abs(v) > 1:
        print(f"    {k:<26} {v:>18,}")
    else:
        print(f"    {k:<26} {v}")

print()
print("【复算】尝试还原每个数字（用公式重算，看是否与接口一致）")
recall = bs["model_recall"]
precision = bs["model_precision"]
churn_n = bs["annual_churn_count"]
print(f"    1) annual_churn_count = total × churn_rate")
print(f"       = {N:,} × {ov['churn_rate']/100:.4f} = {int(N*ov['churn_rate']/100):,}"
      f"   （接口给 {churn_n:,}）")

print(f"    2) annual_flagged = churn_count × recall / precision")
calc_flag = churn_n * recall / precision
print(f"       = {churn_n:,} × {recall} / {precision} = {calc_flag:,.0f}"
      f"   （接口给 {bs['annual_flagged']:,}）")
print(f"       对照：total × coverage = {N:,} × {cov} = {N*cov:,.0f}")

print(f"    3) retained_customers = churn_count × recall")
calc_ret = churn_n * recall
print(f"       = {churn_n:,} × {recall} = {calc_ret:,.0f}"
      f"   （接口给 {bs['retained_customers']:,}）")

print(f"    4) ROI = reduced_loss / intervention_cost")
calc_roi = (calc_ret * bs["avg_customer_value"]) / (calc_flag * bs["cost_per_intervention"])
print(f"       推得的等价式：ROI = COST_RATIO × precision = 5 × {precision} = {5*precision:.2f}"
      f"   （接口给 {bs['roi']}）")
print("       ⚠ 该式成立的前提：每个被触达的 TP 都 100% 挽留成功")

print()
print("=" * 82)
print("三、模型推算 vs 工单实测")
print("=" * 82)
print(f"    【推算值】basis={bs['basis']}")
print(f"        触达 {bs['annual_flagged']:,} 人次，挽回 {bs['retained_customers']:,} 人")
print(f"        推算挽留成功率 = retained/flagged = {bs['retained_customers']/bs['annual_flagged']*100:.1f}%"
      f"（= precision，即'触达的人里确实是流失者的比例'）")
print(f"        ROI = {bs['roi']}")

rs = get("/api/cost-benefit/retention-summary")
print()
print(f"    【实测值】basis={rs['basis']}  has_data={rs['has_data']}")
if rs["has_data"]:
    print(f"        完成工单 {rs['total_completed']} 条，挽回 {rs['retained']} 人，"
          f"流失 {rs['lost']} 人")
    print(f"        实测挽留成功率 = {rs['success_rate']*100:.1f}%")
    print(f"        ROI = {rs.get('roi')}")
    print()
    print(f"    ★ 差异：推算假设 100% 成功，实测 {rs['success_rate']*100:.1f}%"
          f" —— 相差 {100/(rs['success_rate']*100):.1f} 倍")
    print()
    print("    按策略分组的实测成功率:")
    for r in rs["by_strategy"][:6]:
        print(f"      {str(r['strategy'])[:28]:<30} {r['retained']}/{r['total']}"
              f"  {r['success_rate']*100:5.1f}%")
    print("    按负责人:")
    for r in rs["by_assignee"][:6]:
        print(f"      {str(r['assignee'])[:12]:<14} {r['retained']}/{r['total']}"
              f"  {r['success_rate']*100:5.1f}%")

print()
print("=" * 82)
print("四、数据是否有'年度'维度")
print("=" * 82)
cust = get("/api/customers?page=1&page_size=1")
item = cust["items"][0]
print(f"    客户字段: {sorted(item.keys())}")
has_time = [k for k in item if "date" in k.lower() or "time" in k.lower()
            or "year" in k.lower() or "month" in k.lower()]
print(f"    含时间语义的字段: {has_time or '（无）'}")
print("    ⚠ churn_rate 是从**当前快照**算的存量比例，不是年度发生率")
print("       接口却命名为 annual_churn_count / annual_loss / annual_churn_rate")
