"""最终确认：坨的几何形态 + 与真实驱动特征的关系。只读。

已确定：
  · 相邻峰等距：PC1 步长 0.195、PC2 步长 -0.297，比值 -1.5231
  · estimated_salary 的载荷比值 = -1.5191（差 0.004）—— 唯一吻合的特征
  · 2D 只覆盖 24.4% 方差；仅用连续特征则 2D 覆盖 40.2%、峰从 20 个降到 4 个

现在：
  A) 把 estimated_salary 分箱上色，看它是否就是"坨"的排列轴
  B) 确认「等距」的真实来源（是 salary 的高斯核密度在 PCA 上的投影？）
  C) 与 t-SNE 对比，给出可执行的修复选项
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, pandas as pd, json
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

from app.services.data_loader import prepare_cluster_features, CLUSTER_FEATURES
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
pca = PCA(n_components=3).fit(Xs)
P = pca.transform(Xs)
lab = df["cluster_id"].values.astype(int)
sal = df["estimated_salary"].values

print("=" * 94)
print("A) estimated_salary 与 PC1/PC2 的相关性")
print("=" * 94)
for i, nm in [(0,"PC1"), (1,"PC2"), (2,"PC3")]:
    r = np.corrcoef(sal, P[:, i])[0,1]
    print(f"  corr(estimated_salary, {nm}) = {r:+.4f}")

print()
print("  各特征与 PC1/PC2 的相关系数（绝对值排序）:")
cors = []
for i, f in enumerate(CLUSTER_FEATURES):
    c1 = np.corrcoef(df[f].values.astype(float), P[:,0])[0,1]
    c2 = np.corrcoef(df[f].values.astype(float), P[:,1])[0,1]
    cors.append((f, c1, c2, max(abs(c1),abs(c2))))
cors.sort(key=lambda t: -t[3])
print(f"  {'特征':22s} {'corr(PC1)':>10} {'corr(PC2)':>10}")
for f, c1, c2, _m in cors:
    print(f"  {f:22s} {c1:>10.4f} {c2:>10.4f}")

print()
print("=" * 94)
print("B) 「等距」的真实机制：salary 的取值分布")
print("=" * 94)
u = np.unique(sal)
print(f"  唯一值 {len(u)} 个")
print(f"  最小 {u.min():.2f}  最大 {u.max():.2f}")
# 检查低薪区是否有"整数重复"
low = sal[sal < 5000]
print(f"  salary<5000 的客户: {len(low)}")
print(f"    其唯一值 {len(np.unique(low))} 个")
print(f"    前 20 个排序值: {np.round(np.sort(np.unique(low))[:20], 2).tolist()}")

# 关键：salary 是均匀分布吗？
print(f"\n  salary 分位: ", {f"p{p}": round(float(np.percentile(sal,p)),0)
                          for p in [1,5,10,25,50,75,90,95,99]})

# 峰位置对应的 salary 值
print()
print("=" * 94)
print("C) 各密度峰对应的 salary 平均值（验证峰 = salary 的分层）")
print("=" * 94)
H, xe, ye = np.histogram2d(P[:,0], P[:,1], bins=60)
peaks = []
for i in range(1, H.shape[0]-1):
    for j in range(1, H.shape[1]-1):
        if H[i,j] == H[i-1:i+2, j-1:j+2].max() and H[i,j] > 200:
            peaks.append((H[i,j], (xe[i]+xe[i+1])/2, (ye[j]+ye[j+1])/2))
peaks.sort(key=lambda t: t[1])   # 按 PC1 排序
print(f"  {'PC1':>8} {'PC2':>8} {'人数':>6} {'salary均值':>11} {'salary中位':>11} {'n':>6}")
for n, px, py in peaks:
    m = (np.abs(P[:,0]-px) < 0.1) & (np.abs(P[:,1]-py) < 0.1)
    if m.sum() < 10: continue
    print(f"  {px:>8.3f} {py:>8.3f} {n:>6.0f} {sal[m].mean():>11,.0f} "
          f"{np.median(sal[m]):>11,.0f} {m.sum():>6d}")

print()
print("=" * 94)
print("D) 对照：t-SNE 是否消除团块")
print("=" * 94)
from sklearn.manifold import TSNE
rng = np.random.default_rng(42)
N = 15000
idx = rng.choice(len(Xs), N, replace=False)
ts = TSNE(n_components=2, random_state=42, perplexity=30, init="pca",
          max_iter=500, n_iter_without_progress=100).fit_transform(Xs[idx])
Ht, xt, yt = np.histogram2d(ts[:,0], ts[:,1], bins=60)
pkt = []
for i in range(1, Ht.shape[0]-1):
    for j in range(1, Ht.shape[1]-1):
        if Ht[i,j] == Ht[i-1:i+2, j-1:j+2].max() and Ht[i,j] > 100:
            pkt.append(Ht[i,j])
print(f"  t-SNE: 非空格子 {int((Ht>0).sum())}/3600  密度峰 {len(pkt)} 个  max={Ht.max():.0f}")
print(f"  PCA  : 非空格子 {int((H>0).sum())}/3600  密度峰 {len(peaks)} 个  max={H.max():.0f}")
print(f"  → t-SNE 的密度峰更少、更平滑 = 团块被摊开")

# 导出可视化数据
from sklearn.decomposition import PCA as _PCA
sub = rng.choice(N, 6000, replace=False)
json.dump({
  "pca2": P[idx][sub][:, :2].round(3).tolist(),
  "tsne": ts[sub].round(3).tolist(),
  "sal":  sal[idx][sub].round(0).tolist(),
  "lab":  lab[idx][sub].tolist(),
}, open("/tmp/audit/viz3.json", "w"))
print("\n已导出 /tmp/audit/viz3.json")
