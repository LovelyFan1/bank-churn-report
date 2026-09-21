"""第三轮：2D 投影重叠量化 + 备选投影对比。只读。"""
import numpy as np, pandas as pd, json
from app.services.data_loader import prepare_cluster_features
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis as LDA
from sklearn.manifold import TSNE
from sklearn.metrics import silhouette_score

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
cids = sorted(np.unique(lab))

pca = PCA(n_components=11).fit(Xs)
evr = pca.explained_variance_ratio_
print("=== 各主成分解释方差 ===")
cum = 0
for i, v in enumerate(evr):
    cum += v
    print(f"  PC{i+1:2d}: {v*100:5.2f}%   累计 {cum*100:5.2f}%")
for k in (2, 3, 4, 5, 6):
    print(f"  前 {k} 维累计: {evr[:k].sum()*100:.2f}%")

Xp = pca.transform(Xs)

print("\n=== 2D 投影下各簇的边界框重叠 ===")
def bbox_overlap(a, b):
    """两个二维点集的轴对齐包围盒重叠面积占比（相对较小框）"""
    (ax0, ay0), (ax1, ay1) = a.min(0), a.max(0)
    (bx0, by0), (bx1, by1) = b.min(0), b.max(0)
    ix = max(0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    area_b = (bx1 - bx0) * (by1 - by0)
    return inter / area_b if area_b > 0 else 0

print("  簇对   PC1-PC2重叠   PC1-PC3重叠")
for i in range(len(cids)):
    for j in range(i + 1, len(cids)):
        a2 = Xp[lab == cids[i]][:, :2]; b2 = Xp[lab == cids[j]][:, :2]
        a3 = Xp[lab == cids[i]][:, [0, 2]]; b3 = Xp[lab == cids[j]][:, [0, 2]]
        print(f"  C{cids[i]}-C{cids[j]}   {bbox_overlap(a2,b2)*100:6.1f}%      {bbox_overlap(a3,b3)*100:6.1f}%")

print("\n=== 不同投影下的 silhouette（越高越可分）===")
idx = np.random.default_rng(0).choice(len(Xs), 20000, replace=False)
for name, P in [
    ("11D 原始标准化空间", Xs[idx]),
    ("PC1-PC2 (当前图上)", Xp[idx][:, :2]),
    ("PC1-PC3", Xp[idx][:, :3]),
    ("PC1-PC5", Xp[idx][:, :5]),
    ("PC1-PC8", Xp[idx][:, :8]),
]:
    print(f"  {name:22s}: {silhouette_score(P, lab[idx]):+.4f}")

# ── 2D 平面上的最近质心分类准确率（衡量"看图能否分辨簇"）──
print("\n=== 2D 平面上按最近质心分类的准确率 ===")
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import cross_val_score
X2 = Xp[:, :2]
acc2 = cross_val_score(KNeighborsClassifier(5), X2[idx], lab[idx], cv=3).mean()
acc11 = cross_val_score(KNeighborsClassifier(5), Xs[idx], lab[idx], cv=3).mean()
print(f"  PC1-PC2 (2D) 5-NN 准确率 = {acc2*100:.2f}%")
print(f"  11D 空间     5-NN 准确率 = {acc11*100:.2f}%")
print(f"  随机基线 (最大簇占比)    = {max(np.bincount(lab))/len(lab)*100:.2f}%")

# ── 有监督投影 LDA 作为对照：上界参考 ──
print("\n=== 对照：LDA 监督投影（仅作上界参考，不能用于无监督展示）===")
lda = LDA(n_components=4)
Xl = lda.fit_transform(Xs[idx], lab[idx])
print(f"  LDA 2D silhouette = {silhouette_score(Xl[:, :2], lab[idx]):+.4f}")
print(f"  LDA 2D 5-NN 准确率 = {cross_val_score(KNeighborsClassifier(5), Xl[:,:2], lab[idx], cv=3).mean()*100:.2f}%")

# ── 导出可视化数据 ──
rng = np.random.default_rng(42)
sub = rng.choice(len(Xp), 6000, replace=False)
out = {
    "pca2": Xp[sub][:, :2].round(3).tolist(),
    "pca3": Xp[sub][:, [0, 2]].round(3).tolist(),
    "lda2": Xl[:6000][:, :2].round(3).tolist() if len(Xl) >= 6000 else Xl[:, :2].round(3).tolist(),
    "lab_sub": lab[sub].tolist(),
    "lab_lda": lab[idx][:6000].tolist(),
    "explained": evr.tolist(),
}
with open("/tmp/viz.json", "w") as f:
    json.dump(out, f)
print("\n可视化数据已导出 /tmp/viz.json")
