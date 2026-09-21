"""验证「坨」的真实来源：离散特征形成的**一维格点序列**。

观察（diag43）：
  · 20 个密度峰不是散乱分布，而是**沿一条斜线排列**
    （-1.13,-0.98) → (-0.94,-1.28) → (-1.33,-0.69) → (-1.52,-0.39) → (-1.72,-0.09)
    PC1 递减时 PC2 递增 —— 明显的**一维流形**（一条线）
  · 每个峰里 C3:C0 比例稳定在 70:30，说明峰不是"簇"，而是**格点**
  · 80 个离散组合、全量 10 万人落在 80 个格点上，40 个格点人数≥1000

本脚本验证：把每个格点（离散特征的唯一组合）投影到 PC1-PC2，
看它们是否**恰好落在那些峰上**。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, pandas as pd
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

from app.services.data_loader import prepare_cluster_features
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
pca = PCA(n_components=3).fit(Xs)
P = pca.transform(Xs)
lab = df["cluster_id"].values.astype(int)

print("=" * 94)
print("一、每个「格点」在 PC1-PC2 上的位置（格点=离散特征唯一组合）")
print("=" * 94)
disc = ["num_products", "has_credit_card", "is_active_member", "satisfaction_score"]
df2 = df.copy()
df2["_pc1"] = P[:, 0]; df2["_pc2"] = P[:, 1]; df2["_cl"] = lab
g = df2.groupby(disc, observed=True).agg(
    n=("_pc1", "size"), pc1=("_pc1", "mean"), pc2=("_pc2", "mean"),
    pc1_std=("_pc1", "std"), pc2_std=("_pc2", "std")).reset_index()
g = g.sort_values("n", ascending=False)
print(f"  格点数 = {len(g)}")
print(f"  {'n':>6} {'PC1':>8} {'PC2':>8} {'pc1_std':>8} {'pc2_std':>8}  组合")
for _, r in g.head(15).iterrows():
    combo = f"prod={int(r['num_products'])} card={int(r['has_credit_card'])} " \
            f"act={int(r['is_active_member'])} sat={int(r['satisfaction_score'])}"
    print(f"  {int(r['n']):>6} {r['pc1']:>8.2f} {r['pc2']:>8.2f} "
          f"{r['pc1_std']:>8.2f} {r['pc2_std']:>8.2f}  {combo}")

print()
print(f"  格点内 PC1 标准差均值 = {g['pc1_std'].mean():.3f}")
print(f"  格点内 PC2 标准差均值 = {g['pc2_std'].mean():.3f}")
print(f"  全部点 PC1 标准差     = {P[:,0].std():.3f}")
print(f"  全部点 PC2 标准差     = {P[:,1].std():.3f}")
print(f"  → 格点内离散度 / 全局离散度 = "
      f"{g['pc1_std'].mean()/P[:,0].std():.3f} (PC1), "
      f"{g['pc2_std'].mean()/P[:,1].std():.3f} (PC2)")

print()
print("=" * 94)
print("二、格点是否**恰好**位于密度峰上")
print("=" * 94)
# 找出实测峰位置
H, xe, ye = np.histogram2d(P[:,0], P[:,1], bins=60)
peaks = []
for i in range(1, H.shape[0]-1):
    for j in range(1, H.shape[1]-1):
        if H[i,j] == H[i-1:i+2, j-1:j+2].max() and H[i,j] > 200:
            peaks.append((H[i,j], (xe[i]+xe[i+1])/2, (ye[j]+ye[j+1])/2))
peaks.sort(reverse=True)
print(f"  实测密度峰 {len(peaks)} 个")
# 每个大格点中心到最近峰的距离
big = g[g["n"] >= 1000]
dists = []
for _, r in big.iterrows():
    d = min(np.hypot(r["pc1"]-px, r["pc2"]-py) for _n, px, py in peaks)
    dists.append(d)
print(f"  人数>=1000 的格点 {len(big)} 个")
print(f"  其中心到最近密度峰的距离: mean={np.mean(dists):.3f} "
      f"max={np.max(dists):.3f}  (<0.1 即视为重合)")
print(f"  距离<0.15 的比例: {np.mean(np.array(dists)<0.15)*100:.1f}%")

print()
print("=" * 94)
print("三、贡献 PC1/PC2 的特征里，哪些是离散的")
print("=" * 94)
from app.services.data_loader import CLUSTER_FEATURES
L = pd.DataFrame(pca.components_.T, index=CLUSTER_FEATURES, columns=["PC1","PC2","PC3"])
L["|PC1|+|PC2|"] = L["PC1"].abs() + L["PC2"].abs()
L["离散?"] = ["连续","连续","离散(0-10)","连续(含大量0)","离散(1-4)",
              "离散(0/1)","离散(0/1)","连续","离散(1-5)","连续","连续"]
print(L.round(3).sort_values("|PC1|+|PC2|", ascending=False).to_string())

print()
print("=" * 94)
print("四、决定性实验：只保留**连续**特征做 PCA，坨是否消失")
print("=" * 94)
cont = ["credit_score", "age", "balance", "estimated_salary",
        "points_earned", "balance_salary_ratio"]
Xc = prepare_cluster_features(df)[:, [CLUSTER_FEATURES.index(f) for f in cont]]
sc2 = StandardScaler(); Xc2 = sc2.fit_transform(Xc)
p2 = PCA(n_components=3).fit(Xc2)
Pc = p2.transform(Xc2)
Hc, xec, yec = np.histogram2d(Pc[:,0], Pc[:,1], bins=60)
pk = []
for i in range(1, Hc.shape[0]-1):
    for j in range(1, Hc.shape[1]-1):
        if Hc[i,j] == Hc[i-1:i+2, j-1:j+2].max() and Hc[i,j] > 200:
            pk.append(Hc[i,j])
print(f"  仅连续特征(6维): 解释方差 2D={p2.explained_variance_ratio_[:2].sum():.4f}")
print(f"    非空格子 {int((Hc>0).sum())}/3600  密度峰 {len(pk)} 个  max={Hc.max():.0f}")
print(f"  vs 全部11维  : 解释方差 2D={pca.explained_variance_ratio_[:2].sum():.4f}")
print(f"    非空格子 {int((H>0).sum())}/3600  密度峰 {len(peaks)} 个  max={H.max():.0f}")

print()
print("=" * 94)
print("五、PC1/PC2 上「相邻峰」的间距 —— 是否等于某个离散特征的步长")
print("=" * 94)
ps = np.array([[px, py] for _n, px, py in peaks[:8]])
print("  前 8 个峰坐标:")
for i, (px, py) in enumerate(ps):
    print(f"    #{i}: PC1={px:7.3f}  PC2={py:7.3f}")
# 相邻峰间距
d = [np.hypot(*(ps[i+1]-ps[i])) for i in range(len(ps)-1)]
print(f"  相邻峰间距: {[round(x,3) for x in d]}")
print("  → 若间距**近似等距**，说明是离散特征的等差步长在 PCA 上的投影")

# 按 PC1 排序看是否等距
order = np.argsort(ps[:,0])
ps_sorted = ps[order]
print(f"\n  按 PC1 排序后: {[round(x,3) for x in ps_sorted[:,0]]}")
print(f"  PC1 差分: {[round(x,3) for x in np.diff(ps_sorted[:,0])]}")
print(f"  PC2 差分: {[round(x,3) for x in np.diff(ps_sorted[:,1])]}")
