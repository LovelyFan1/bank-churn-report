"""核查：当前「决策线」选出的名单里，有多少人按个体经济算是**净亏**的。

疑点（来自 diag87 的观察）：
  排序用的是 expected_value = p × balance（期望损失）
  但干预是要花钱的：cost = AVG_CUSTOMER_VALUE / COST_RATIO = 50,000/5 = 10,000
  而成功率只有 s=0.30

  个体决策应有：
      期望挽回 = p × balance × s
      净收益   = p × balance × s − c
      当且仅当 p × balance > c/s 才值得干预

  而 aggregate 的 net_profit 用的是 AVG_CUSTOMER_VALUE（5 万）作为
  所有人的统一价值 —— 两者口径是否一致？名单里有没有"该剔除"的人？

本脚本在真实 96,418 行上算清楚。
"""
import json

import numpy as np
import urllib.request

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r:
        return json.loads(r.read().decode())


ri = get("/api/model/risk-info")
AVG = get("/api/dashboard/summary")["business_summary"]["avg_customer_value"]
CR = ri["cost_ratio"]
S = ri["success_rate"]
DT = ri["decision_threshold"]
COV = ri["decision_coverage"]

cost_per = AVG / CR
breakeven_ev = cost_per / S

print("=" * 84)
print("参数")
print("=" * 84)
print(f"  单次干预成本 c        = AVG/COST_RATIO = {AVG:,.0f}/{CR} = {cost_per:,.0f}")
print(f"  挽留成功率 s          = {S}")
print(f"  决策阈值（概率线）    = {DT}   覆盖率 {COV*100:.2f}%")
print(f"  个体盈亏平衡点        = c/s = {breakeven_ev:,.0f}")
print(f"    → 即 expected_value(p×balance) 需 > {breakeven_ev:,.0f} 才值得干预")

# ── 取全量客户（分页拿不到 96k，改用导出接口的替代：直接抽样决策线内的人）──
# 用 top_n 接口取概率最高的若干；但更准确的是遍历客户列表。
# 这里用「按概率排序」分页抓取决策线内的人（p >= DT）
print()
print("=" * 84)
print("抓取决策线内的客户（p >= 阈值）")
print("=" * 84)

in_line = []
page = 1
while True:
    d = get(f"/api/customers?page={page}&page_size=100&sort_by=probability&sort_order=desc")
    items = d["items"]
    if not items:
        break
    stop = False
    for c in items:
        if c["probability"] < DT:
            stop = True
            break
        in_line.append(c)
    if stop or page * 100 >= d["total"]:
        break
    page += 1
    if page > 120:   # 安全上限
        break

print(f"  抓到决策线内客户 {len(in_line):,} 人（期望约 {int(96418*COV):,}，抽样上限所致）")

if not in_line:
    raise SystemExit("未抓到样本")

ev = np.array([c["expected_value"] for c in in_line], dtype=float)
bal = np.array([c["balance"] for c in in_line], dtype=float)
p = np.array([c["probability"] for c in in_line], dtype=float)

# 个体净收益（元）：p × balance × s − c
net = p * bal * S - cost_per

worth = net > 0
n_worth = int(worth.sum())
n_total = len(in_line)

print()
print("=" * 84)
print("结论：决策线内，按个体经济是否有净收益")
print("=" * 84)
print(f"  样本数            {n_total:,}")
print(f"  值得干预(净>0)    {n_worth:,}  ({n_worth/n_total*100:.1f}%)")
print(f"  净亏(净<=0)       {n_total-n_worth:,}  ({(n_total-n_worth)/n_total*100:.1f}%)")
print()
print(f"  净收益合计        {net.sum():,.0f} 元")
print(f"  若剔除净亏者后    {net[worth].sum():,.0f} 元")
print(f"  剔除反而使总额    {'增加' if net[worth].sum() > net.sum() else '减少'}"
      f" {abs(net[worth].sum()-net.sum()):,.0f} 元")
print(f"  浪费的干预成本    {abs(net[~worth].sum()):,.0f} 元"
      f"（{n_total-n_worth:,} 次 × {cost_per:,.0f} 的成本中，收不回的部分）")

print()
print("=" * 84)
print("净亏者的画像")
print("=" * 84)
bad = [c for c, w in zip(in_line, worth) if not w]
if bad:
    b_bal = np.array([c["balance"] for c in bad])
    b_ev = np.array([c["expected_value"] for c in bad])
    b_p = np.array([c["probability"] for c in bad])
    from collections import Counter
    tiers = Counter(c["value_tier"] for c in bad)
    print(f"  人数          {len(bad):,}")
    print(f"  平均余额      {b_bal.mean():,.0f}    中位 {np.median(b_bal):,.0f}")
    print(f"  平均概率      {b_p.mean():.4f}")
    print(f"  平均期望价值  {b_ev.mean():,.0f}")
    print(f"  价值层分布    {dict(tiers)}")
    print()
    print("  样例（净收益从低到高）:")
    bad_sorted = sorted(bad, key=lambda c: c["probability"] * c["balance"] * S - cost_per)
    for c in bad_sorted[:6]:
        n = c["probability"] * c["balance"] * S - cost_per
        print(f"    {c['customer_id']}  p={c['probability']:.4f}  "
              f"余额={c['balance']:>12,.0f}  EV={c['expected_value']:>11,.0f}  "
              f"净={n:>11,.0f}  层={c['value_tier']}")

print()
print("=" * 84)
print("为什么会这样（口径不一致）")
print("=" * 84)
print(f"  · 排序/筛选用的是 expected_value = p × balance（**期望损失**）")
print(f"  · 干预成本参照的是 p × balance × s（**期望挽回**）")
print(f"  · 两者差一个 s={S} 的系数，再减成本 c={cost_per:,.0f}")
print(f"  · 边界差：EV 需 > {breakeven_ev:,.0f}，而当前只要求 p >= {DT}")
print()
print(f"  决策线的概率切点 {DT} 是**全局**成本最优（用人均价值 {AVG:,.0f} 算），")
print(f"  它不区分「这位客户余额只有 3 万」还是「余额 20 万」——")
print(f"  对所有 TP 一律记 +(s×COST_RATIO−1) = {S*CR-1:+.1f} 个成本单位。")
print()
print("  ⇒ 所以名单里必然混入**余额低到不值得打**的人。")
