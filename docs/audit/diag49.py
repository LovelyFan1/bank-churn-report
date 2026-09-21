"""决定性地重定位：4 条带 vs 团块。只读。

已排除：
  · "等距峰"是**方形网格与斜向条带相交**的产物（峰间距随 bins 变化：
    0.39/0.292/0.234/0.195/0.146/0.117，恰好 ≈ 网格宽度）
  · 一维 PC1 密度只有 4 个峰、间距不等距（std=0.146）

新假设：**num_products 的 4 个取值 → 图上 4 条斜带**。
  各层 PC1 均值:  1→+0.63, 2→-0.60, 3→-1.21, 4→-1.74
  一维密度峰:      +0.19, -0.63, -1.33, -1.79
  两组数对得上。

本脚本验证：
  A) 每层（num_products=k）在平面的形状：是细带还是圆团？
  B) 层内是否还能看到更细的结构（其他离散特征）
  C) 定量：4 条带的「带内离散度 / 带间距」
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

from app.services.data_loader import prepare_cluster_features, CLUSTER_FEATURES
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
pca = PCA(n_components=3).fit(Xs)
P = pca.transform(Xs)

print("=" * 94)
print("A) 按 num_products 分层：每层形状（细带 or 圆团）")
print("=" * 94)
print(f"  {'k':>3} {'n':>7} {'PC1mean':>8} {'PC1std':>7} {'PC2mean':>8} {'PC2std':>7} "
      f"{'形状比值':>9}")
layers = {}
for k in sorted(df["num_products"].unique()):
    m = df["num_products"].values == k
    a, b = P[m,0], P[m,1]
    # 用 PCA 再看该层的主轴：若第一主成分占绝对优势 → 细带
    Z = np.c_[a, b]
    Zc = Z - Z.mean(0)
    cov = np.cov(Zc.T)
    w, v = np.linalg.eigh(cov)
    w = np.sort(w)[::-1]
    ratio = w[0] / w[1] if w[1] > 0 else np.inf
    layers[k] = (a.mean(), b.mean(), a.std(), b.std(), ratio)
    print(f"  {k:>3} {m.sum():>7d} {a.mean():>8.3f} {a.std():>7.3f} "
          f"{b.mean():>8.3f} {b.std():>7.3f} {ratio:>9.2f}")
print()
print("  形状比值 = 层内第一主轴方差 / 第二主轴方差")
print("  → 比值 >> 1 表示该层是**细长条带**；≈1 表示圆形团")

print()
print("=" * 94)
print("B) 各层中心的连线方向 vs 层内展开方向")
print("=" * 94)
ks = sorted(layers)
centers = np.array([[layers[k][0], layers[k][1]] for k in ks])
print("  各层中心:")
for i, k in enumerate(ks):
    print(f"    k={k}: PC1={centers[i,0]:7.3f}  PC2={centers[i,1]:7.3f}")
d = np.diff(centers, axis=0)
print(f"  相邻层中心位移矢量: {[tuple(np.round(x,3)) for x in d]}")
print(f"  位移方向是否一致: "
      f"{'✅ 是（4 层排成一条直线）' if np.allclose(d[0], d[1], atol=0.15) and np.allclose(d[1], d[2], atol=0.15) else '部分'}")
print(f"  层间距: {[round(float(np.hypot(*x)),3) for x in d]}")

# 层内主轴方向
print()
for k in ks:
    m = df["num_products"].values == k
    Z = P[m][:, :2] - P[m][:, :2].mean(0)
    cov = np.cov(Z.T)
    w, v = np.linalg.eigh(cov)
    idx = np.argsort(w)[::-1]
    main = v[:, idx[0]]
    ang = np.degrees(np.arctan2(main[1], main[0]))
    print(f"  k={k}: 层内主轴方向角 = {ang:7.1f}°   "
          f"主轴 std={np.sqrt(w[idx[0]]):.3f}  次轴 std={np.sqrt(w[idx[1]]):.3f}")

if len(centers) >= 2:
    cd = centers[-1] - centers[0]
    cang = np.degrees(np.arctan2(cd[1], cd[0]))
    print(f"\n  层中心连线方向角 = {cang:.1f}°")
    print("  → 若层内主轴角 ≈ 层中心连线角，说明**各层沿同一条线排开**，")
    print("     整体是一条被切成 4 段的斜带，而非 4 个独立圆团")

print()
print("=" * 94)
print("C) 一维 PC1 密度：4 个峰与 num_products 的对应")
print("=" * 94)
h, e = np.histogram(P[:,0], bins=100)
ctr = (e[:-1]+e[1:])/2
pk = []
for i in range(1, len(h)-1):
    if h[i] > h[i-1] and h[i] > h[i+1] and h[i] > h.mean()*1.1:
        pk.append((ctr[i], h[i]))
print(f"  PC1 密度峰: {[(round(x,3), int(y)) for x,y in pk]}")
print(f"  num_products 各层 PC1 均值: "
      f"{[(k, round(layers[k][0],3)) for k in ks]}")

# 用 num_products 分层后的 PC1 分布，检查是否重叠
print()
print("  各层 PC1 的 [p5, p95] 区间（看是否重叠）:")
for k in ks:
    m = df["num_products"].values == k
    a = P[m,0]
    print(f"    k={k}: [{np.percentile(a,5):7.2f}, {np.percentile(a,95):7.2f}]  "
          f"median={np.median(a):7.2f}")

print()
print("=" * 94)
print("D) 定量总结：为什么看起来「一坨一坨」")
print("=" * 94)
sil2 = None
# 层间距离 vs 层内离散度
dists = [float(np.hypot(*x)) for x in d]
instd = np.mean([np.hypot(layers[k][2], layers[k][3]) for k in ks])
print(f"  层间中心距离（均值） = {np.mean(dists):.3f}")
print(f"  层内离散度（PC1/PC2 std 的模） = {instd:.3f}")
print(f"  信噪比 = {np.mean(dists)/instd:.3f}")
print()
print("  方差覆盖:")
evr = pca.explained_variance_ratio_
print(f"    PC1+PC2 = {evr[:2].sum()*100:.2f}%  → 丢失 {(1-evr[:2].sum())*100:.2f}%")
print(f"  主导特征（|载荷|之和 > 0.3）:")
for i, f in enumerate(CLUSTER_FEATURES):
    v = abs(pca.components_[0,i]) + abs(pca.components_[1,i])
    if v > 0.3:
        disc = "离散" if f in ("num_products","has_credit_card","is_active_member",
                              "satisfaction_score","tenure") else "连续"
        print(f"    {f:22s} {v:.3f}  ({disc})")
