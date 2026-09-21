"""第九轮：K 值全面诊断 —— 多指标 + 稳定性 + 业务可解释性。只读。

⚠ 与之前的做法不同：本脚本同时评估三个层面，而不是只看 silhouette。
   1) 几何指标：inertia(肘部) / silhouette / Calinski-Harabasz / Davies-Bouldin
   2) 稳定性指标（对业务最关键）：同一 k 用不同种子重聚类，两两 ARI
      —— 若换一次随机种子划分就变，这个 k 不能用于决策
   3) 业务可解释性：各簇流失率是否拉得开、大小是否均衡
"""
import numpy as np, pandas as pd
from app.services.data_loader import prepare_cluster_features
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import (silhouette_score, calinski_harabasz_score,
                             davies_bouldin_score, adjusted_rand_score)

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

X = prepare_cluster_features(df)
Xs = StandardScaler().fit_transform(X)
y = df["exited"].values
print(f"样本数={len(Xs)}, 维度={Xs.shape[1]}, 整体流失率={y.mean()*100:.2f}%\n")

# 用固定抽样（5 万）保证各 k 可比；指标计算再抽样以控时
rng = np.random.default_rng(42)
idx = rng.choice(len(Xs), 50000, replace=False)
Xc, yc = Xs[idx], y[idx]

K_RANGE = [2, 3, 4, 5, 6, 7, 8]
SEEDS = [0, 1, 2, 3, 4]

print("=" * 92)
print("一、几何指标（单个固定种子 42 拟合）")
print("=" * 92)
geo = []
for k in K_RANGE:
    km = MiniBatchKMeans(n_clusters=k, random_state=42, batch_size=10000, n_init=10)
    lb = km.fit_predict(Xc)
    sil = silhouette_score(Xc, lb, sample_size=20000, random_state=42)
    ch = calinski_harabasz_score(Xc, lb)
    db_ = davies_bouldin_score(Xc, lb)
    geo.append({"k": k, "inertia": km.inertia_, "silhouette": sil,
                "CH": ch, "DB": db_})
g = pd.DataFrame(geo)
# 肘部的"拐点"：inertia 下降率
g["inertia_drop%"] = (-g["inertia"].diff() / g["inertia"].shift(1) * 100).round(2)
print(g.to_string(index=False,
      formatters={"inertia": "{:,.0f}".format, "silhouette": "{:+.4f}".format,
                  "CH": "{:,.0f}".format, "DB": "{:.4f}".format}))

print("\n  各指标最优 k：")
print(f"    silhouette 最大     → k={g.loc[g['silhouette'].idxmax(),'k']}")
print(f"    Calinski-Harabasz 最大 → k={g.loc[g['CH'].idxmax(),'k']}")
print(f"    Davies-Bouldin 最小  → k={g.loc[g['DB'].idxmin(),'k']}")
print(f"    inertia 拐点(降幅骤减) → 见上表 inertia_drop%")

print()
print("=" * 92)
print("二、稳定性指标（5 个种子两两 ARI）—— 对业务分群最关键")
print("=" * 92)
print("  ARI=1 表示两个种子的划分完全一致；<0.5 表示划分随种子大幅变动\n")
stab = []
for k in K_RANGE:
    labels_list = []
    for s in SEEDS:
        km = MiniBatchKMeans(n_clusters=k, random_state=s, batch_size=10000, n_init=10)
        labels_list.append(km.fit_predict(Xc))
    aris = []
    for i in range(len(SEEDS)):
        for j in range(i + 1, len(SEEDS)):
            aris.append(adjusted_rand_score(labels_list[i], labels_list[j]))
    stab.append({"k": k, "ARI_mean": np.mean(aris), "ARI_min": np.min(aris),
                 "ARI_max": np.max(aris), "ARI_std": np.std(aris)})
s = pd.DataFrame(stab)
print(s.to_string(index=False, formatters={
    "ARI_mean": "{:+.4f}".format, "ARI_min": "{:+.4f}".format,
    "ARI_max": "{:+.4f}".format, "ARI_std": "{:.4f}".format}))
best_stab = s.loc[s["ARI_mean"].idxmax(), "k"]
print(f"\n  稳定性最优 k = {best_stab}  (ARI_mean={s['ARI_mean'].max():.4f})")

print()
print("=" * 92)
print("三、业务可解释性：各簇流失率能否拉开")
print("=" * 92)
biz = []
for k in K_RANGE:
    km = MiniBatchKMeans(n_clusters=k, random_state=42, batch_size=10000, n_init=10)
    lb = km.fit_predict(Xc)
    rates = [yc[lb == c].mean() * 100 for c in range(k)]
    sizes = [(lb == c).sum() for c in range(k)]
    biz.append({
        "k": k,
        "churn极差pp": round(max(rates) - min(rates), 2),
        "churn标准差": round(float(np.std(rates)), 2),
        "整体churn%": round(float(yc.mean() * 100), 2),
        "最大簇占比%": round(max(sizes) / len(lb) * 100, 1),
        "最小簇占比%": round(min(sizes) / len(lb) * 100, 1),
        "最小簇人数": int(min(sizes)),
    })
b = pd.DataFrame(biz)
print(b.to_string(index=False))

print("\n  各簇流失率明细：")
for k in K_RANGE:
    km = MiniBatchKMeans(n_clusters=k, random_state=42, batch_size=10000, n_init=10)
    lb = km.fit_predict(Xc)
    rates = [f"{yc[lb==c].mean()*100:5.1f}" for c in range(k)]
    print(f"    k={k}: [{', '.join(rates)}]")

print()
print("=" * 92)
print("四、综合评分（各指标归一化后加权）")
print("=" * 92)

def norm(series, higher_better=True):
    v = series.astype(float)
    r = (v - v.min()) / (v.max() - v.min()) if v.max() > v.min() else v * 0
    return r if higher_better else 1 - r

score = pd.DataFrame({"k": g["k"]})
# 权重依据：业务分群首要看稳定性，其次看簇间流失率差异，再次才是几何指标
score["稳定性(ARI)"]   = norm(s["ARI_mean"], True)          * 0.40
score["业务区分度(流失率极差)"] = norm(b["churn极差pp"], True)  * 0.30
score["silhouette"]   = norm(g["silhouette"], True)        * 0.15
score["CH"]           = norm(g["CH"], True)                * 0.10
score["DB(越小越好)"]  = norm(g["DB"], False)               * 0.05
score["综合得分"] = score[["稳定性(ARI)","业务区分度(流失率极差)",
                          "silhouette","CH","DB(越小越好)"]].sum(axis=1)
print(score.round(4).to_string(index=False))
winner = int(score.loc[score["综合得分"].idxmax(), "k"])
print(f"\n  ⭐ 综合最优 k = {winner}")
print(f"     次优 k = {int(score.sort_values('综合得分', ascending=False).iloc[1]['k'])}")
