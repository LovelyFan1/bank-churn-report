"""第七轮：簇命名规则失效的根因分析 —— 阈值 vs 真实分布。只读。"""
import numpy as np, pandas as pd
from app.database import SessionLocal
from app.models.customer import Customer

db = SessionLocal()
rows = db.query(Customer).all()
df = pd.DataFrame([{
    "credit_score": r.credit_score, "age": r.age, "tenure": r.tenure,
    "balance": r.balance, "num_products": r.num_products,
    "has_credit_card": r.has_credit_card, "is_active_member": r.is_active_member,
    "estimated_salary": r.estimated_salary, "exited": r.exited,
    "cluster_id": r.cluster_id,
} for r in rows])
db.close()

# 复刻 get_cluster_names 的画像口径
feats = ["balance", "num_products", "is_active_member", "estimated_salary"]
prof = {}
for cid in sorted(df["cluster_id"].unique()):
    sub = df[df["cluster_id"] == cid]
    prof[int(cid)] = {
        "count": len(sub),
        "churn": sub["exited"].mean() * 100,
        "balance": sub["balance"].mean(),
        "products": sub["num_products"].mean(),
        "active": sub["is_active_member"].mean(),
        "salary": sub["estimated_salary"].mean(),
    }

print("=" * 78)
print("一、规则里的 7 个阈值，在真实数据里各处于什么位置？")
print("=" * 78)

rules = [
    ("churn > 40",        "各簇流失率",       [p["churn"] for p in prof.values()]),
    ("balance > 120000",  "balance 均值",     [p["balance"] for p in prof.values()]),
    ("num_products > 2.5","num_products 均值",[p["products"] for p in prof.values()]),
    ("is_active < 0.2",   "活跃度均值",       [p["active"] for p in prof.values()]),
    ("salary < 50000",    "salary 均值",      [p["salary"] for p in prof.values()]),
    ("active > 0.8",      "活跃度均值",       [p["active"] for p in prof.values()]),
    ("churn < 15",        "各簇流失率",       [p["churn"] for p in prof.values()]),
    ("churn < 10",        "各簇流失率",       [p["churn"] for p in prof.values()]),
]
print(f"{'规则':22s} {'阈值':>10s}  簇值范围              实际命中簇数")
for rule, label, vals in rules:
    thr = float(rule.split()[-1])
    lo, hi = min(vals), max(vals)
    if ">" in rule.split()[1]:
        hit = sum(1 for v in vals if v > thr)
    else:
        hit = sum(1 for v in vals if v < thr)
    print(f"  {rule:22s} {thr:8.0f}   [{lo:8.2f}, {hi:8.2f}]   {hit}/5")

print()
print("=" * 78)
print("二、逐簇「为什么落到兜底分支」—— 差多少就能命中上一条")
print("=" * 78)

def explain(cid, p):
    """按 get_cluster_names 的顺序逐步判定，并报告每条差多少"""
    churn, bal, prod, act, sal = p["churn"], p["balance"], p["products"], p["active"], p["salary"]
    steps = [
        ("churn > 40",            churn - 40,          churn > 40),
        ("balance > 120000",      bal - 120000,        bal > 120000),
        ("num_products > 2.5",    prod - 2.5,          prod > 2.5),
        ("is_active < 0.2",       0.2 - act,           act < 0.2),
        ("salary < 50000",        50000 - sal,         sal < 50000),
        ("active>0.8 and churn<15", min(act - 0.8, 15 - churn), act > 0.8 and churn < 15),
        ("churn < 10",            10 - churn,          churn < 10),
    ]
    print(f"\n  ── C{cid} (n={p['count']}, 流失 {churn:.2f}%, 余额 {bal:,.0f}, "
          f"产品 {prod:.2f}, 活跃 {act:.2f}, 薪资 {sal:,.0f}) ──")
    for name, margin, ok in steps:
        mark = "✅命中" if ok else ("❌差 " + (f"{abs(margin):,.2f}" if abs(margin) < 1e6 else f"{abs(margin):,.0f}"))
        print(f"     {name:26s} 余量={margin:>12,.2f}  {mark}")
        if ok:
            print(f"     → 判定为「{name}」分支，命名结束")
            return name
    print(f"     → 全部不命中，落到 else 兜底「中等价值客户」")
    return "中等价值客户(兜底)"

results = {}
for cid, p in prof.items():
    results[cid] = explain(cid, p)

print()
print("=" * 78)
print("三、结果汇总")
print("=" * 78)
for cid, nm in results.items():
    print(f"  C{cid}: {nm}")

print()
print("=" * 78)
print("四、关键：这些阈值在**全体数据**中的分位位置")
print("=" * 78)
allbal = df["balance"]
print(f"  balance 全体：median={allbal.median():,.0f}  p75={allbal.quantile(.75):,.0f}  "
      f"p90={allbal.quantile(.90):,.0f}  p95={allbal.quantile(.95):,.0f}  max={allbal.max():,.0f}")
print(f"    → 阈值 120000 处于全体 balance 的 **{ (allbal < 120000).mean()*100:.1f}% 分位**")
print(f"    → 全体中 balance>120000 的客户仅占 **{(allbal > 120000).mean()*100:.1f}%**")
print(f"    → 全体中 balance==0 的客户占 **{(allbal == 0).mean()*100:.1f}%**")

p_ = df["num_products"]
print(f"\n  num_products 全体：mean={p_.mean():.2f}  max={p_.max()}  取值={sorted(p_.unique())}")
print(f"    → 阈值 2.5 意味「产品数≥3」；全体中占比 **{(p_ > 2.5).mean()*100:.1f}%**")

a = df["is_active_member"]
print(f"\n  is_active_member 全体：mean={a.mean():.2f}（0/1 变量）")
print(f"    → 阈值 0.2 是**簇均值**的判定，全体活跃率 {a.mean()*100:.1f}%")

s = df["estimated_salary"]
print(f"\n  estimated_salary 全体：median={s.median():,.0f}  p25={s.quantile(.25):,.0f}")
print(f"    → 阈值 50000 处于 **{(s < 50000).mean()*100:.1f}% 分位**")

print()
print("=" * 78)
print("五、如果改成「相对命名」（按各簇指标排序），结果会是什么")
print("=" * 78)
t = pd.DataFrame(prof).T
t.index = [f"C{i}" for i in t.index]
print(t.round(2).to_string())

# 给每个簇按各维度排名
print("\n  各簇排名（1=该维度最高）:")
rank = pd.DataFrame({
    "流失率排名(高→低)": t["churn"].rank(ascending=False).astype(int),
    "余额排名(高→低)":   t["balance"].rank(ascending=False).astype(int),
    "活跃度排名(高→低)": t["active"].rank(ascending=False).astype(int),
    "薪资排名(高→低)":   t["salary"].rank(ascending=False).astype(int),
})
print(rank.to_string())
