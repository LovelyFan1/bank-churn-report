"""第五轮：PC 之间冗余度 + 分块 winsorize 对 scaler 的影响。只读。"""
import numpy as np, pandas as pd
from app.services.data_loader import (
    prepare_cluster_features, CLUSTER_FEATURES, winsorize_cluster_features,
    DataLoader, CLUSTER_WINSOR_QUANTILE,
)
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

db = SessionLocal()
loader = DataLoader(db)
rows = db.query(Customer).all()
df = pd.DataFrame([{
    "credit_score": r.credit_score, "age": r.age, "tenure": r.tenure,
    "balance": r.balance, "num_products": r.num_products,
    "has_credit_card": r.has_credit_card, "is_active_member": r.is_active_member,
    "estimated_salary": r.estimated_salary, "exited": r.exited,
    "satisfaction_score": r.satisfaction_score, "points_earned": r.points_earned,
    "balance_salary_ratio": r.balance_salary_ratio, "cluster_id": r.cluster_id,
} for r in rows])

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
pca = PCA(n_components=3).fit(Xs)
Xp = pca.transform(Xs)

print("=== 主成分之间的相关性（|r| 高 = 信息冗余）===")
C = np.corrcoef(Xp[:, :3].T)
print(f"  r(PC1,PC2) = {C[0,1]:+.4f}")
print(f"  r(PC1,PC3) = {C[0,2]:+.4f}")
print(f"  r(PC2,PC3) = {C[1,2]:+.4f}")

print("\n=== 载荷矩阵（看 PC1/PC2 是否由同一组特征主导）===")
L = pd.DataFrame(pca.components_.T, index=CLUSTER_FEATURES,
                 columns=["PC1", "PC2", "PC3"])
L["PC1_abs+PC2_abs"] = L["PC1"].abs() + L["PC2"].abs()
print(L.round(3).sort_values("PC1_abs+PC2_abs", ascending=False).to_string())

# 主导特征
top1 = L["PC1"].abs().sort_values(ascending=False).head(3).index.tolist()
top2 = L["PC2"].abs().sort_values(ascending=False).head(3).index.tolist()
print(f"\n  PC1 主导特征: {top1}")
print(f"  PC2 主导特征: {top2}")
print(f"  交集: {sorted(set(top1) & set(top2))}  ← 交集越大，两轴信息越重复")

print("\n=== 分块 winsorize 不一致的真实影响 ===")
cs = loader.chunksize
# 任务里 scaler 只用前 3 chunk 拟合
sample_offsets = list(range(0, loader.total_count, cs))[:3]
scaler_task = StandardScaler()
scaler_task.fit(np.vstack([prepare_cluster_features(loader.load_chunk(o))
                           for o in sample_offsets]))

# 对比：用全量 winsorize 拟合的 scaler
scaler_full = StandardScaler().fit(X)

print("  balance_salary_ratio 的缩放尺度：")
print(f"    任务流程 scale = {scaler_task.scale_[CLUSTER_FEATURES.index('balance_salary_ratio')]:.4f}")
print(f"    全量流程 scale = {scaler_full.scale_[CLUSTER_FEATURES.index('balance_salary_ratio')]:.4f}")

# 用任务 scaler 变换 chunk0 和 chunk1，看同一列被缩放到什么分布
print("\n  同一列（balance_salary_ratio）在不同 chunk 上被缩放的均值：")
for i, ch in enumerate(loader.iter_chunks()):
    w = winsorize_cluster_features(ch)
    z = (w["balance_salary_ratio"] - scaler_task.mean_[CLUSTER_FEATURES.index("balance_salary_ratio")]) \
        / scaler_task.scale_[CLUSTER_FEATURES.index("balance_salary_ratio")]
    print(f"    chunk{i}: 截断后max={w['balance_salary_ratio'].max():7.3f}  z后max={z.max():7.3f}  z均值={z.mean():.4f}")

print("\n=== 结论性检查：PCA 空间与聚类空间是否一致 ===")
print(f"  聚类用的空间: {CLUSTER_FEATURES}  (11 维)")
print(f"  图上展示的空间: PC1-PC2 (2 维, 覆盖 {pca.explained_variance_ratio_[:2].sum()*100:.2f}% 方差)")
print(f"  丢失信息: {(1-pca.explained_variance_ratio_[:2].sum())*100:.2f}%")

# 各簇质心在 11D 与 2D 的距离保持性
print("\n=== 簇间距离保持性（11D 真实距离 vs 2D 图上距离）===")
cents11 = np.array([Xs[df["cluster_id"].values == c].mean(0) for c in sorted(df["cluster_id"].unique())])
cents2 = np.array([Xp[df["cluster_id"].values == c][:, :2].mean(0) for c in sorted(df["cluster_id"].unique())])
from scipy.spatial.distance import pdist, squareform
d11 = squareform(pdist(cents11)); d2 = squareform(pdist(cents2))
mask = np.triu(np.ones_like(d11), 1).astype(bool)
r = np.corrcoef(d11[mask], d2[mask])[0, 1]
print(f"  簇间距离相关系数 r = {r:+.4f}  (1.0 = 完美保持, 0 = 完全无关)")
print("\n  真实 11D 簇间距离矩阵:")
print(pd.DataFrame(d11, index=[f"C{c}" for c in range(5)],
                   columns=[f"C{c}" for c in range(5)]).round(2).to_string())
print("\n  2D 图上簇间距离矩阵:")
print(pd.DataFrame(d2, index=[f"C{c}" for c in range(5)],
                   columns=[f"C{c}" for c in range(5)]).round(2).to_string())

db.close()
