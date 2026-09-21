"""排除「峰是分箱假象」的可能 + 定位等距的真实机制。只读。

前面用 60×60 方形网格看到 20 个等距峰。
必须排除：若数据沿斜线分布，方形网格会天然产生规则交点。

检验方法：换多个网格尺寸 / 换六边形分箱 / 直接看沿主轴的密度曲线，
         若峰位置**稳定不随分箱变化**，则是真峰。
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

print("=" * 94)
print("一、换不同网格尺寸，峰的位置是否稳定")
print("=" * 94)
for B in [30, 40, 50, 60, 80, 100]:
    H, xe, ye = np.histogram2d(P[:,0], P[:,1], bins=B)
    pk = []
    for i in range(1, B-1):
        for j in range(1, B-1):
            if H[i,j] == H[i-1:i+2, j-1:j+2].max() and H[i,j] > len(P)/B/B*4:
                pk.append((xe[i]+xe[i+1])/2)
    pk.sort()
    d = np.diff(pk)
    print(f"  bins={B:4d}: 峰数={len(pk):3d}  PC1 位置={[round(x,2) for x in pk[:8]]}")
    if len(d) > 3:
        print(f"             间距={[round(x,3) for x in d[:6]]}")

print()
print("=" * 94)
print("二、沿 PC1 主轴的**一维**密度曲线（完全不涉及二维分箱）")
print("=" * 94)
h, e = np.histogram(P[:,0], bins=100)
ctr = (e[:-1]+e[1:])/2
# 找峰
pk1 = []
for i in range(1, len(h)-1):
    if h[i] > h[i-1] and h[i] > h[i+1] and h[i] > h.mean()*1.15:
        pk1.append(ctr[i])
print(f"  PC1 一维密度峰 {len(pk1)} 个")
print(f"    位置: {[round(x,3) for x in pk1]}")
if len(pk1) > 2:
    d = np.diff(pk1)
    print(f"    间距: {[round(x,3) for x in d]}")
    print(f"    间距 std = {np.std(d):.4f}  mean={np.mean(d):.4f}")
    print(f"    → {'✅ 一维上也等距，不是二维分箱假象' if np.std(d) < 0.05 else '⚠ 一维上不等距'}")

print()
print("=" * 94)
print("三、换成六边形分箱（彻底排除方形网格的规则交点）")
print("=" * 94)
try:
    from matplotlib.path import Path as MPath
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.patches import RegularPolygon
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
print(f"  matplotlib 可用: {HAS_MPL}")

# 不依赖 matplotlib：用圆盘核密度估计
rng = np.random.default_rng(0)
samp = P[rng.choice(len(P), 30000, replace=False)][:, :2]
from scipy.spatial import cKDTree
tree = cKDTree(samp)
# 网格点上算 k 近邻距离（越小越密）
gx = np.linspace(P[:,0].min(), P[:,0].max(), 120)
gy = np.linspace(P[:,1].min(), P[:,1].max(), 120)
GX, GY = np.meshgrid(gx, gy)
pts = np.c_[GX.ravel(), GY.ravel()]
# 过滤掉远离数据区的网格点
d_far, _ = tree.query(pts, k=1)
inside = d_far < 0.25
d5, _ = tree.query(pts[inside], k=6)
dens = np.zeros(len(pts)); dens[inside] = 1.0/(d5[:, -1] + 1e-6)
D = dens.reshape(GX.shape)
print(f"  核密度网格 120×120，有效点 {inside.sum()}")

# 在密度场里找峰
pks = []
for i in range(2, D.shape[0]-2):
    for j in range(2, D.shape[1]-2):
        w = D[i-2:i+3, j-2:j+3]
        if D[i,j] == w.max() and D[i,j] > np.percentile(D[inside.reshape(D.shape)], 92):
            pks.append((D[i,j], gx[j], gy[i]))
pks.sort(reverse=True)
# 去重（邻近峰合并）
merged = []
for v, x, y in pks:
    if all(np.hypot(x-mx, y-my) > 0.3 for _v, mx, my in merged):
        merged.append((v, x, y))
print(f"  去重后峰 {len(merged)} 个")
ms = sorted(merged, key=lambda t: t[1])
print(f"  {'PC1':>8} {'PC2':>8} {'密度':>10}")
for v, x, y in ms[:12]:
    print(f"  {x:>8.3f} {y:>8.3f} {v:>10.2f}")
if len(ms) > 2:
    d1 = np.diff([x for _v, x, _y in ms])
    print(f"  PC1 间距: {[round(x,3) for x in d1[:8]]}")
    print(f"  → {'✅ 核密度法也得到等距峰' if np.std(d1) < 0.06 else '⚠ 核密度法下不等距'}")

print()
print("=" * 94)
print("四、机制定位：薪水与「峰」的对应关系")
print("=" * 94)
sal = df["estimated_salary"].values
# 按 salary 分 20 层，看每层的 PC1/PC2 中心
bins = np.linspace(sal.min(), sal.max(), 21)
idx = np.digitize(sal, bins)
print(f"  {'salary区间':>20} {'n':>6} {'PC1均值':>9} {'PC2均值':>9} {'PC1 std':>8}")
rowsout = []
for b in range(1, 21):
    m = idx == b
    if m.sum() < 30: continue
    lo, hi = bins[b-1], bins[b]
    rowsout.append((lo, hi, m.sum(), P[m,0].mean(), P[m,1].mean(), P[m,0].std()))
    print(f"  [{lo:9,.0f},{hi:9,.0f}) {m.sum():>6d} {P[m,0].mean():>9.3f} "
          f"{P[m,1].mean():>9.3f} {P[m,0].std():>8.3f}")
print()
d1 = np.diff([r[3] for r in rowsout])
print(f"  各 salary 层的 PC1 均值差分: {[round(x,3) for x in d1]}")
print(f"  差分 std = {np.std(d1):.4f}")
print("  → 若差分近似恒定，说明 salary 在 PC1 上是近似**线性增加**的")
print("     而峰对应的是 salary 分布**自身的密度起伏**（非等差）")

# salary 自身的密度
h2, e2 = np.histogram(sal, bins=40)
print(f"\n  salary 自身直方图(40箱) 是否多峰:")
pk2 = [i for i in range(1,39) if h2[i] > h2[i-1] and h2[i] > h2[i+1] and h2[i] > h2.mean()*1.1]
print(f"    峰数 = {len(pk2)}   计数范围=[{h2.min()},{h2.max()}]")
print(f"    → salary 是均匀分布（生成器 uniform(10000,200000)），")
print(f"      自身**不应**有多峰。若图上多峰，则来自其他维度与它的交互。")
