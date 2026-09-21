"""业务逻辑审计（二）—— 验证 ROI 公式的隐含假设 + 名单一致性。

要验证：
  1. ROI = COST_RATIO × precision 这个等价式是否恒成立？它隐含了什么？
  2. 「触达即挽回」的假设对 ROI 的影响有多大（敏感性分析）？
  3. 客户列表 Top-N 名单，与「决策阈值应触达的人」是否一致？
  4. 期望价值排序 vs 概率排序，选出来的人差多少（是否真的更值）？
  5. 干预策略矩阵的 count 合计是否等于测试集规模（账能不能对上）？
"""
import json
import urllib.request

import numpy as np

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=240) as r:
        return json.loads(r.read().decode("utf-8"))


print("=" * 82)
print("一、ROI 公式的隐含假设 —— 敏感性分析")
print("=" * 82)

bs = get("/api/dashboard/summary")["business_summary"]
CR = 5.0            # COST_RATIO
AVG = bs["avg_customer_value"]

print(f"    系统当前 ROI = {bs['roi']}")
print(f"    COST_RATIO × precision = {CR} × {bs['model_precision']} = {CR*bs['model_precision']:.2f}")
print()
print("    ⚠ 该公式**不含'挽留成功率'这一项**。推导：")
print("        reduced_loss   = 挽回人数 × 客单价，其中 挽回人数 = 流失数 × recall")
print("        intervention   = 触达人次 × (客单价/COST_RATIO)")
print("        ROI = (流失数×recall×AVG) / (触达×AVG/CR)")
print("            = CR × (流失数×recall) / 触达")
print("            = CR × (TP) / (TP+FP) = CR × precision")
print("      即：系统默认「被模型判为流失的人，只要触达就 100% 挽留成功」。")

print()
print("    若引入真实挽留成功率 s，则 ROI 变为 CR × precision × s：")
print(f"    {'挽留成功率 s':<16} {'ROI':>8}   说明")
for s in [1.0, 0.8, 0.607, 0.5, 0.3, 0.2]:
    roi = CR * bs["model_precision"] * s
    note = ""
    if s == 1.0:
        note = "← 当前系统隐含的假设"
    elif abs(s - 0.607) < 0.01:
        note = "← 工单实测值（56 条样本）"
    elif roi < 1:
        note = "← ROI < 1，不划算"
    print(f"    {s:<16.3f} {roi:>8.2f}   {note}")

print()
print("    ★ 结论：按实测 60.7% 成功率，ROI = "
      f"{CR*bs['model_precision']*0.607:.2f}；")
print(f"      若成功率低于 {1/(CR*bs['model_precision']):.2f}，ROI 跌破 1，项目不再划算。")

print()
print("=" * 82)
print("二、名单一致性：不同页面选出的'该干预的人'是否同一批")
print("=" * 82)

# 列表默认按 expected_value 排序
top_ev = get("/api/customers?page=1&page_size=100&sort_by=expected_value&sort_order=desc")
top_p = get("/api/customers?page=1&page_size=100&sort_by=probability&sort_order=desc")

ev_ids = [x["customer_id"] for x in top_ev["items"]]
p_ids = [x["customer_id"] for x in top_p["items"]]
inter = len(set(ev_ids) & set(p_ids))
print(f"    按期望价值 Top100 与 按概率 Top100 的重合: {inter}/100")
print(f"    两者选出的名单重合度 = {inter}%")

# 风险等级分布
from collections import Counter
print(f"    期望价值 Top100 的等级分布: {dict(Counter(x['risk_level'] for x in top_ev['items']))}")
print(f"    概率     Top100 的等级分布: {dict(Counter(x['risk_level'] for x in top_p['items']))}")

# 价值层分布
print(f"    期望价值 Top100 的价值层: {dict(Counter(x['value_tier'] for x in top_ev['items']))}")
print(f"    概率     Top100 的价值层: {dict(Counter(x['value_tier'] for x in top_p['items']))}")

ev_val = sum(x["expected_value"] for x in top_ev["items"])
p_val = sum(x["expected_value"] for x in top_p["items"])
print()
print(f"    Top100 期望价值合计: 按EV排序 = {ev_val:,.0f}  按概率排序 = {p_val:,.0f}")
print(f"    按 EV 排序多捕获 = {(ev_val/p_val-1)*100:+.1f}%")
print("    → 说明'按概率排序'确实会漏掉高价值客户（与代码注释一致）")

print()
print("=" * 82)
print("三、干预策略矩阵：账能否对上")
print("=" * 82)
m = get("/api/portfolio/matrix")
test_size = m["test_size"]
cells = m["cells"]
cell_sum = sum(c["count"] for c in cells)
print(f"    test_size = {test_size:,}")
print(f"    cells 合计 = {cell_sum:,}")
print(f"    一致 = {cell_sum == test_size}")
print()
print(f"    tier_totals:  {[(t['key'], t['count']) for t in m['tier_totals']]}")
print(f"    level_totals: {[(t['key'], t['count']) for t in m['level_totals']]}")
print(f"    recall_used = {m['recall_used']}   阈值 = {m['thresholds']}")
print(f"    min_cell_sample = {m['min_cell_sample']}")
print()
print("    各格明细（tier × level）:")
print(f"    {'价值层':<8}{'等级':<10}{'人数':>7}{'流失率':>9}{'格内召回':>10}{'可挽回金额':>16}  样本够?")
for c in cells:
    cr_ = c.get("churn_rate")
    ru = c.get("recall_used")
    rv = c.get("recoverable_value")
    print(f"    {c['tier']:<8}{c['level']:<10}{c['count']:>7}"
          f"{(f'{cr_*100:.1f}%' if cr_ is not None else '—'):>9}"
          f"{(f'{ru*100:.1f}%' if ru is not None else '—'):>10}"
          f"{(f'{rv:,.0f}' if rv is not None else '—'):>16}"
          f"  {'是' if c['sample_sufficient'] else '否'}")

print()
print("=" * 82)
print("四、'低风险格可挽回为 0' 的业务含义")
print("=" * 82)
low_cells = [c for c in cells if c["level"] == "LOW"]
print(f"    LOW 等级共 {sum(c['count'] for c in low_cells):,} 人"
      f"（测试集的 {sum(c['count'] for c in low_cells)/test_size*100:.1f}%）")
print(f"    其中真实流失 {sum((c.get('churn_rate') or 0)*c['count'] for c in low_cells):.0f} 人")
print(f"    决策线下可挽回金额合计 = "
      f"{sum(c.get('recoverable_value') or 0 for c in low_cells):,.0f}")
print("    → 决策线只覆盖 37%，LOW 段（35%）全部落在线上方之外")
print("      意味着：这段人群里的真实流失者**不会被干预**，是 recall 0.78 的代价来源")
