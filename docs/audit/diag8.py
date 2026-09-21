"""第八轮：验证「相对命名」方案能否给出 5 个不同簇名 + 边界情况。只读。"""
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
    "satisfaction_score": r.satisfaction_score, "points_earned": r.points_earned,
    "balance_salary_ratio": r.balance_salary_ratio,
    "cluster_id": r.cluster_id,
} for r in rows])
db.close()

prof = {}
for cid in sorted(df["cluster_id"].unique()):
    s = df[df["cluster_id"] == cid]
    prof[int(cid)] = {
        "count": len(s), "churn": s["exited"].mean()*100,
        "balance": s["balance"].mean(), "products": s["num_products"].mean(),
        "active": s["is_active_member"].mean(), "salary": s["estimated_salary"].mean(),
        "age": s["age"].mean(), "credit": s["credit_score"].mean(),
        "tenure": s["tenure"].mean(),
    }
t = pd.DataFrame(prof).T
t.index = [f"C{i}" for i in t.index]

print("=" * 80)
print("一、补充：聚类实际使用但命名规则**完全没用到**的维度")
print("=" * 80)
for f in ["age", "credit", "tenure"]:
    print(f"\n  [{f}]  C0={t.loc['C0',f]:.2f}  C1={t.loc['C1',f]:.2f}  "
          f"C2={t.loc['C2',f]:.2f}  C3={t.loc['C3',f]:.2f}  C4={t.loc['C4',f]:.2f}")
    print(f"        簇间极差={t[f].max()-t[f].min():.2f}  "
          f"（对比 churn 极差={t['churn'].max()-t['churn'].min():.2f}）")

print()
print("=" * 80)
print("二、相对命名方案（按各簇在**本批簇内**的名次判定）")
print("=" * 80)

# 名次（1=最高）
r_churn   = t["churn"].rank(ascending=False)
r_balance = t["balance"].rank(ascending=False)
r_active  = t["active"].rank(ascending=False)
r_salary  = t["salary"].rank(ascending=False)
n = len(t)

print(f"\n  簇数 n={n}，名次阈值：前 40% = 名次 ≤ {np.ceil(n*0.4):.0f}，"
      f"后 40% = 名次 ≥ {n - np.ceil(n*0.4) + 1:.0f}\n")

def rel_name(cid):
    """基于相对名次命名：优先级从'风险'到'价值'"""
    ch = r_churn[cid]; ba = r_balance[cid]; ac = r_active[cid]; sa = r_salary[cid]
    top = np.ceil(n * 0.4)        # 名次 <= top 视为"高"
    bot = n - top + 1             # 名次 >= bot 视为"低"

    # 1) 流失率最高 且 活跃度最低 → 最该干预
    if ch <= top and ac >= bot:
        return "高流失沉默客户"
    # 2) 流失率最高 → 高流失风险
    if ch <= top:
        return "高流失风险客户"
    # 3) 活跃度最低 → 沉默
    if ac >= bot:
        return "低活跃沉默客户"
    # 4) 余额最高 且 薪资最低 → 高余额但低薪（可能是老年/低收客户）
    if ba <= top and sa >= bot:
        return "高余额低薪客户"
    # 5) 余额最高 → 高价值
    if ba <= top:
        return "高余额价值客户"
    # 6) 活跃度最高 且 流失率最低 → 稳定
    if ac <= top and ch >= bot:
        return "高活跃稳定客户"
    # 7) 薪资最高 → 高薪
    if sa <= top:
        return "高薪优质客户"
    # 8) 余额最低 且 活跃度低 → 待激活
    if ba >= bot and ac >= bot:
        return "低余额待激活客户"
    return "中等价值客户"

print("  逐簇判定：\n")
names = {}
for cid in t.index:
    nm = rel_name(cid)
    names[cid] = nm
    print(f"    {cid}: churn#{int(r_churn[cid])} bal#{int(r_balance[cid])} "
          f"act#{int(r_active[cid])} sal#{int(r_salary[cid])}  → 「{nm}」")

print(f"\n  得到的不同簇名数：{len(set(names.values()))} / {n}")
print(f"  名称列表：{list(names.values())}")

print()
print("=" * 80)
print("三、对比：现有绝对阈值规则 vs 相对命名")
print("=" * 80)
old_names = {0: "中等价值客户", 1: "中等价值客户", 2: "低活跃沉默客户",
             3: "中等价值客户", 4: "中等价值客户"}
print(f"\n  {'簇':4s} {'旧名称':20s} {'新名称':20s}")
for cid in t.index:
    c = int(cid[1:])
    print(f"  {cid:4s} {old_names[c]:20s} {names[cid]:20s}")
print(f"\n  旧方案不同名称数 = {len(set(old_names.values()))} / 5")
print(f"  新方案不同名称数 = {len(set(names.values()))} / 5")

print()
print("=" * 80)
print("四、边界检查：若换成 k=3 / k=6，相对命名是否仍有效")
print("=" * 80)
from app.services.data_loader import prepare_cluster_features
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import MiniBatchKMeans

X = prepare_cluster_features(df)
Xs = StandardScaler().fit_transform(X)
for k in [3, 4, 6]:
    km = MiniBatchKMeans(n_clusters=k, random_state=42, batch_size=10000, n_init=3)
    lb = km.fit_predict(Xs)
    p2 = {}
    for c in sorted(np.unique(lb)):
        s = df[lb == c]
        p2[int(c)] = {"count": len(s), "churn": s["exited"].mean()*100,
                      "balance": s["balance"].mean(),
                      "active": s["is_active_member"].mean(),
                      "salary": s["estimated_salary"].mean()}
    tt = pd.DataFrame(p2).T
    rc = tt["churn"].rank(ascending=False); rb = tt["balance"].rank(ascending=False)
    ra = tt["active"].rank(ascending=False); rs = tt["salary"].rank(ascending=False)
    nn = len(tt); tp = np.ceil(nn*0.4); bt = nn - tp + 1
    got = []
    for c in tt.index:
        if rc[c] <= tp and ra[c] >= bt: got.append("高流失沉默客户")
        elif rc[c] <= tp: got.append("高流失风险客户")
        elif ra[c] >= bt: got.append("低活跃沉默客户")
        elif rb[c] <= tp and rs[c] >= bt: got.append("高余额低薪客户")
        elif rb[c] <= tp: got.append("高余额价值客户")
        elif ra[c] <= tp and rc[c] >= bt: got.append("高活跃稳定客户")
        elif rs[c] <= tp: got.append("高薪优质客户")
        elif rb[c] >= bt and ra[c] >= bt: got.append("低余额待激活客户")
        else: got.append("中等价值客户")
    print(f"\n  k={k}: 不同名称 {len(set(got))}/{nn}  →  {got}")
    print(f"        churn 极差={tt['churn'].max()-tt['churn'].min():.2f}pp  "
          f"balance 极差={tt['balance'].max()-tt['balance'].min():,.0f}")
