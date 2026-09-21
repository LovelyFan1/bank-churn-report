"""第四轮：簇命名与实际特征是否吻合 + 载荷解读。只读。"""
import numpy as np, pandas as pd
from app.services.data_loader import prepare_cluster_features, CLUSTER_FEATURES
from app.database import SessionLocal
from app.models.customer import Customer

db = SessionLocal()
rows = db.query(Customer).all()
df = pd.DataFrame([{
    "credit_score": r.credit_score, "age": r.age, "tenure": r.tenure,
    "balance": r.balance, "num_products": r.num_products,
    "has_credit_card": r.has_credit_card, "is_active_member": r.is_active_member,
    "estimated_salary": r.estimated_salary, "exited": r.exited,
    "satisfaction_score": r.satisfaction_score, "points_earned": r.points_earned,
    "balance_salary_ratio": r.balance_salary_ratio, "cluster_id": r.cluster_id,
} for r in rows])
db.close()

print("=== 各簇实际画像（服务端 get_cluster_profiles 的口径）===")
feats = ["credit_score","age","tenure","balance","num_products",
         "estimated_salary","satisfaction_score","is_active_member"]
rowsout = []
for cid in sorted(df["cluster_id"].unique()):
    sub = df[df["cluster_id"] == cid]
    rec = {"cluster": int(cid), "count": len(sub),
           "churn%": round(sub["exited"].mean()*100, 2)}
    for f in feats:
        rec[f] = round(sub[f].mean(), 2)
    rowsout.append(rec)
t = pd.DataFrame(rowsout)
print(t.to_string(index=False))

print("\n=== 按 get_cluster_names 的规则逐条判定 ===")
for _, r in t.iterrows():
    cid = int(r["cluster"]); churn = r["churn%"]
    bal = r["balance"]; prod = r["num_products"]
    act = r["is_active_member"]; sal = r["estimated_salary"]
    if churn > 40:            nm = "高流失风险客户"
    elif bal > 120000:        nm = "高余额价值客户"
    elif prod > 2.5:          nm = "多产品忠诚客户"
    elif act < 0.2:           nm = "低活跃沉默客户"
    elif sal < 50000:         nm = "低薪价格敏感客户"
    elif act > 0.8 and churn < 15: nm = "高活跃稳定客户"
    elif churn < 10:          nm = "低风险优质客户"
    else:                     nm = "中等价值客户"
    print(f"  C{cid}: churn={churn:5.2f}%  bal={bal:9.0f}  prod={prod:.2f}  "
          f"active={act:.2f}  sal={sal:8.0f}  → 「{nm}」")

print("\n=== 全行流失率基准 ===")
print(f"  整体 churn = {df['exited'].mean()*100:.2f}%")
print(f"  各簇 churn 极差 = {t['churn%'].max() - t['churn%'].min():.2f} 个百分点")
print(f"  各簇 churn 标准差 = {t['churn%'].std():.2f}")

print("\n=== 簇大小均衡度 ===")
print(f"  最大簇 {t['count'].max()} / 最小簇 {t['count'].min()} = {t['count'].max()/t['count'].min():.2f}x")

print("\n=== balance / estimated_salary / num_products 的簇间差异 ===")
for f in ["balance","estimated_salary","num_products","is_active_member","age"]:
    g = df.groupby("cluster_id")[f].mean()
    print(f"  {f:20s} 簇间极差 = {g.max()-g.min():12.2f}   簇内std均值/簇间std = "
          f"{df.groupby('cluster_id')[f].std().mean()/g.std():.2f}")
