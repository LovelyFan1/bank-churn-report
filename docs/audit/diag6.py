"""第六轮：替代投影方案对比 —— 找出能让簇可分辨的画法。只读。"""
import numpy as np, pandas as pd, json
from app.services.data_loader import prepare_cluster_features, CLUSTER_FEATURES
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
from sklearn.metrics import silhouette_score
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import cross_val_score

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
sc = StandardScaler(); Xs = sc.fit_transform(X)
lab = df["cluster_id"].values.astype(int)

# 用 3 万抽样做投影对比（t-SNE 全量太慢）
rng = np.random.default_rng(42)
idx = rng.choice(len(Xs), 30000, replace=False)
Xs_s, lab_s = Xs[idx], lab[idx]

def report(name, P):
    sil = silhouette_score(P, lab_s, sample_size=15000, random_state=42)
    acc = cross_val_score(KNeighborsClassifier(5), P, lab_s, cv=3,
                          n_jobs=1).mean()
    print(f"  {name:34s} silhouette={sil:+.4f}  5-NN={acc*100:5.2f}%")
    return sil, acc

print("=== 各投影方案（3 万抽样）===")
print(f"  随机基线（最大簇占比）              = {max(np.bincount(lab_s))/len(lab_s)*100:5.2f}%\n")

pca_full = PCA(n_components=11, random_state=42).fit(Xs_s)
P_all = pca_full.transform(Xs_s)
report("PC1-PC2（当前页面）", P_all[:, :2])
report("PC1-PC3", P_all[:, [0, 2]])
report("PC2-PC3", P_all[:, [1, 2]])
report("PC1-PC4", P_all[:, :4])
report("PC1-PC6", P_all[:, :6])
report("11D 全空间", Xs_s)

print("\n=== t-SNE（2D，可视化专用）===")
N_TS = 15000
ts = TSNE(n_components=2, random_state=42, perplexity=30,
          init="pca", max_iter=500, n_iter_without_progress=100).fit_transform(Xs_s[:N_TS])
labor = lab_s[:N_TS]
sil_ts = silhouette_score(ts, labor, sample_size=10000, random_state=42)
acc_ts = cross_val_score(KNeighborsClassifier(5), ts, labor, cv=3, n_jobs=1).mean()
print(f"  {'t-SNE 2D (perplexity=30)':34s} silhouette={sil_ts:+.4f}  5-NN={acc_ts*100:5.2f}%")

print("\n=== 关键：聚类本身的质量（K 值是否合理）===")
from sklearn.cluster import KMeans, MiniBatchKMeans
for k in [2, 3, 4, 5, 6, 8]:
    km = MiniBatchKMeans(n_clusters=k, random_state=42, batch_size=5000, n_init=3)
    lb = km.fit_predict(Xs_s)
    sil = silhouette_score(Xs_s, lb, sample_size=15000, random_state=42)
    print(f"  k={k}: silhouette={sil:+.4f}  inertia={km.inertia_:.0f}")

print("\n=== 标签的稳定性检验（5 次不同种子重聚类，ARI 与现标签比较）===")
from sklearn.metrics import adjusted_rand_score
for seed in [0, 1, 2]:
    km = MiniBatchKMeans(n_clusters=5, random_state=seed, batch_size=5000, n_init=3)
    lb = km.fit_predict(Xs_s)
    print(f"  seed={seed}: ARI vs 现有标签 = {adjusted_rand_score(lab_s, lb):+.4f}")

# 导出 t-SNE 供可视化
sub = rng.choice(len(ts), 6000, replace=False)
json.dump({
    "tsne": ts[sub].round(3).tolist(),
    "lab": labor[sub].tolist(),
    "pca2": P_all[:N_TS][sub][:, :2].round(3).tolist(),
}, open("/tmp/viz2.json", "w"))
print("\n已导出 /tmp/viz2.json")
