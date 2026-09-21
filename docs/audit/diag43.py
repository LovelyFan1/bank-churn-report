"""PCA 散点「一坨一坨」的成因诊断。只读。

观察：页面上散点呈现明显的团块（clump）结构。
本脚本量化「坨」的个数、位置与成因，不臆测。

待检验的三个假设：
  H1) 团块 = 5 个 K-Means 簇（即聚类本身分开了）
  H2) 团块 = 离散特征构成的格点（has_credit_card / is_active_member /
      num_products / satisfaction_score / tenure 都是有限取值，
      标准化后形成有限个取值组合 → PCA 投影出离散云团）
  H3) 团块 = 某个离群子群（如新分出的 1251 人低薪簇）
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np
import pandas as pd
from sandbox import session
from sqlalchemy import text

db = session()
rows = db.execute(text(
    "SELECT id, credit_score, age, tenure, balance, num_products, "
    "has_credit_card, is_active_member, estimated_salary, exited, "
    "satisfaction_score, points_earned, balance_salary_ratio, cluster_id "
    "FROM customers ORDER BY id")).fetchall()
db.close()
cols = ["id","credit_score","age","tenure","balance","num_products",
        "has_credit_card","is_active_member","estimated_salary","exited",
        "satisfaction_score","points_earned","balance_salary_ratio","cluster_id"]
df = pd.DataFrame(rows, columns=cols)
print(f"读入 {len(df)} 行")

from app.services.data_loader import prepare_cluster_features, CLUSTER_FEATURES
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
pca = PCA(n_components=3).fit(Xs)
P = pca.transform(Xs)
print(f"\n解释方差 PC1={pca.explained_variance_ratio_[0]:.4f} "
      f"PC2={pca.explained_variance_ratio_[1]:.4f} "
      f"PC3={pca.explained_variance_ratio_[2]:.4f} "
      f"| 2D合计={pca.explained_variance_ratio_[:2].sum():.4f}")

lab = df["cluster_id"].values.astype(int)

print()
print("=" * 94)
print("H1) 团块是否 = 5 个 K-Means 簇")
print("=" * 94)
for c in sorted(set(lab)):
    m = lab == c
    print(f"  C{c}: n={m.sum():6d}  PC1=[{P[m,0].min():7.2f},{P[m,0].max():7.2f}] "
          f"mean={P[m,0].mean():7.2f} | PC2 mean={P[m,1].mean():6.2f} "
          f"std={P[m,1].std():5.2f}")
    print(f"        聚类中心(11维) → PC1={pca.transform(np.array([sc.transform([X[m][0]])[0]]))[0][0]:.2f}")

print()
print("  各簇在 PC1-PC2 平面上的**包围盒重叠率**（相对较小框）:")
def overlap(a, b):
    (ax0,ay0),(ax1,ay1) = a.min(0), a.max(0)
    (bx0,by0),(bx1,by1) = b.min(0), b.max(0)
    ix = max(0, min(ax1,bx1)-max(ax0,bx0)); iy = max(0, min(ay1,by1)-max(ay0,by0))
    ab = (bx1-bx0)*(by1-by0)
    return ix*iy/ab if ab > 0 else 0
cs = sorted(set(lab))
for i in range(len(cs)):
    for j in range(i+1, len(cs)):
        a = P[lab==cs[i]][:, :2]; b = P[lab==cs[j]][:, :2]
        print(f"    C{cs[i]}-C{cs[j]}: {overlap(a,b)*100:5.1f}%")

print()
print("=" * 94)
print("H2) 离散特征取值组合数 —— 是否构成格点")
print("=" * 94)
for f in ["num_products", "has_credit_card", "is_active_member",
          "satisfaction_score", "tenure"]:
    u = sorted(df[f].unique())
    print(f"  {f:20s} 取值数={len(u):3d}  {u[:12]}{'...' if len(u)>12 else ''}")

# 关键：这几个离散列的组合数
disc = ["num_products", "has_credit_card", "is_active_member", "satisfaction_score"]
combos = df.groupby(disc, observed=True).size()
print(f"\n  {'×'.join(disc)}")
print(f"  理论组合数 = {df[disc].nunique().prod()}")
print(f"  实际出现的组合数 = {len(combos)}")
print(f"  全量客户 {len(df)} 落在 {len(combos)} 个格点上")
big = combos.sort_values(ascending=False)
print(f"  最大 5 个格点人数: {big.head(5).tolist()}")
print(f"  人数 >= 1000 的格点数: {(big>=1000).sum()}")
print("  → 若格点数很少且每个格点人数很多，则离散特征会在 PCA 上形成**离散云团**")

print()
print("=" * 94)
print("H3) 团块是否来自某个离群子群")
print("=" * 94)
# 在平面上做网格密度，找峰
H, xe, ye = np.histogram2d(P[:,0], P[:,1], bins=60)
print(f"  60×60 网格：非空格子 {int((H>0).sum())} / 3600")
print(f"  单位格子人数: mean={H[H>0].mean():.1f} max={H.max():.0f}")

# 找局部极大（简单 3x3 邻域）
peaks = []
for i in range(1, H.shape[0]-1):
    for j in range(1, H.shape[1]-1):
        w = H[i-1:i+2, j-1:j+2]
        if H[i,j] == w.max() and H[i,j] > 200:
            peaks.append((H[i,j], (xe[i]+xe[i+1])/2, (ye[j]+ye[j+1])/2))
peaks.sort(reverse=True)
print(f"\n  密度峰（>200 人/格）: {len(peaks)} 个")
for n, x, y in peaks[:12]:
    print(f"    人数 {n:6.0f}  位置 PC1={x:7.2f} PC2={y:6.2f}")

# 每个峰里的主导簇
print("\n  前 8 个峰的主导簇构成:")
for n, x, y in peaks[:8]:
    m = (np.abs(P[:,0]-x) < (xe[1]-xe[0])) & (np.abs(P[:,1]-y) < (ye[1]-ye[0]))
    if m.sum() == 0: continue
    vc = pd.Series(lab[m]).value_counts()
    dist = " ".join(f"C{k}:{v}({v/m.sum()*100:.0f}%)" for k, v in vc.items())
    print(f"    PC1={x:6.2f} PC2={y:6.2f} n={m.sum():5d} → {dist}")

print()
print("=" * 94)
print("综合判定")
print("=" * 94)
from sklearn.metrics import silhouette_score
rng = np.random.default_rng(42)
idx = rng.choice(len(P), 20000, replace=False)
sil2 = silhouette_score(P[idx][:, :2], lab[idx])
sil11 = silhouette_score(Xs[idx], lab[idx])
print(f"  silhouette: 11维={sil11:+.4f}  2维(PC1-PC2)={sil2:+.4f}")
print(f"  解释方差 2D 覆盖 = {pca.explained_variance_ratio_[:2].sum()*100:.2f}%")
