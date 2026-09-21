"""PCA 散点图诊断 —— 只读，不修改任何数据。"""
import numpy as np
import pandas as pd
import json
from sqlalchemy import create_engine

from app.services.data_loader import (
    prepare_cluster_features, CLUSTER_FEATURES, winsorize_cluster_features,
    DataLoader,
)
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import silhouette_score

out = {}
db = SessionLocal()
loader = DataLoader(db)
total = loader.total_count
out["total"] = total
out["chunksize"] = loader.chunksize

# ---- 复刻 clustering_service._get_dataframe ----
rows = db.query(Customer).all()
import pandas as pd
df = pd.DataFrame([{
    "id": r.id, "credit_score": r.credit_score, "age": r.age, "tenure": r.tenure,
    "balance": r.balance, "num_products": r.num_products,
    "has_credit_card": r.has_credit_card, "is_active_member": r.is_active_member,
    "estimated_salary": r.estimated_salary, "exited": r.exited,
    "satisfaction_score": r.satisfaction_score, "points_earned": r.points_earned,
    "balance_salary_ratio": r.balance_salary_ratio, "cluster_id": r.cluster_id,
} for r in rows])
print("df shape:", df.shape)
print("cluster_id nulls:", df["cluster_id"].isna().sum())

clustered = df[df["cluster_id"].notna()].copy()
print("clustered:", len(clustered))

# ---- 复刻 get_3d_scatter_data ----
X = prepare_cluster_features(clustered)
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)
pca = PCA(n_components=3)
X_pca = pca.fit_transform(X_scaled)

ev = pca.explained_variance_ratio_
print("\n=== 解释方差 ===")
for i, v in enumerate(ev):
    print(f"  PC{i+1}: {v*100:.2f}%")
print(f"  3D 合计: {ev.sum()*100:.2f}%")
print(f"  2D (图上实际只用 x,y): {(ev[0]+ev[1])*100:.2f}%")

# ---- 各簇在 PC1/PC2 上的分布 ----
print("\n=== 各簇在 PC1 / PC2 / PC3 上的分布 ===")
cids = sorted(clustered["cluster_id"].unique())
labels_arr = clustered["cluster_id"].values
for cid in cids:
    m = labels_arr == cid
    print(f"  C{int(cid)} n={int(m.sum()):6d}  "
          f"PC1 {X_pca[m,0].mean():7.3f}±{X_pca[m,0].std():6.3f}  "
          f"PC2 {X_pca[m,1].mean():7.3f}±{X_pca[m,1].std():6.3f}  "
          f"PC3 {X_pca[m,2].mean():7.3f}±{X_pca[m,2].std():6.3f}")

# ---- 裁剪影响 ----
print("\n=== 坐标轴裁剪影响 ([0.5%,99.5%]) ===")
for axis, idx in (("x", 0), ("y", 1), ("z", 2)):
    col = X_pca[:, idx]
    lo, hi = np.quantile(col, 0.005), np.quantile(col, 0.995)
    outside = ((col < lo) | (col > hi)).sum()
    print(f"  {axis}: [{lo:.4f}, {hi:.4f}]  width={hi-lo:.4f}  "
          f"全range=[{col.min():.2f},{col.max():.2f}]  裁掉{outside}({outside/len(col)*100:.2f}%)")

# ---- 关键：2D 投影下簇是否能分开 ----
print("\n=== 簇分离度（2D 投影, PC1-PC2）===")
sil_2d = silhouette_score(X_pca[:, :2], labels_arr, sample_size=10000, random_state=42)
sil_11d = silhouette_score(X_scaled, labels_arr, sample_size=10000, random_state=42)
print(f"  silhouette (2D 投影) = {sil_2d:.4f}")
print(f"  silhouette (11D 原始) = {sil_11d:.4f}")

# ---- 未截断对比 ----
print("\n=== 若不 winsorize 会怎样 ===")
X_raw = clustered[CLUSTER_FEATURES].values
sc2 = StandardScaler(); Xr = sc2.fit_transform(X_raw)
p2 = PCA(n_components=3); Xr_pca = p2.fit_transform(Xr)
print(f"  未截断 PC1 全range: {Xr_pca[:,0].min():.2f} ~ {Xr_pca[:,0].max():.2f}")
print(f"  未截断 PC2 全range: {Xr_pca[:,1].min():.2f} ~ {Xr_pca[:,1].max():.2f}")
q1 = np.quantile(Xr_pca[:,1], [0.005, 0.995])
print(f"  未截断 PC2 1~99%: {q1[1]-q1[0]:.2f}  占比 {(q1[1]-q1[0])/(Xr_pca[:,1].max()-Xr_pca[:,1].min())*100:.2f}%")
print(f"  未截断解释方差: {p2.explained_variance_ratio_*100}")

# ---- 聚类复现：MiniBatchKMeans 与散点是否同源 ----
print("\n=== 聚类器复现（MiniBatchKMeans, 全量 fit） ===")
mb = MiniBatchKMeans(n_clusters=5, random_state=42, batch_size=10000, n_init=3)
lbl = mb.fit_predict(X_scaled)
print(f"  inertia={mb.inertia_:.2f}  silhouette={silhouette_score(X_scaled, lbl, sample_size=10000, random_state=42):.4f}")
print("  各簇大小:", {int(k): int(v) for k, v in zip(*np.unique(lbl, return_counts=True))})

# 与 DB 中保存的标签比较（用 crosstab）
ct = pd.crosstab(clustered["cluster_id"].values, lbl)
print("\n  与 DB 标签的交叉表 (行=DB, 列=重算):")
print(ct.to_string())

# ---- 载荷 ----
print("\n=== PC1/PC2/PC3 载荷 ===")
load = pd.DataFrame(pca.components_.T, index=CLUSTER_FEATURES,
                    columns=["PC1", "PC2", "PC3"])
print(load.round(3).to_string())

db.close()
