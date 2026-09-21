"""决定性判定：团块 vs 流形串珠。只读。

疑点：前面的"等距峰"可能是我用**方形网格**统计造成的假象 ——
      若数据沿一条斜线分布，斜线穿过方格天然产生等距交点。

要区分两种可能：
  P1) **真团块**：数据在平面上形成若干彼此分离的密集区
      → 表现：PC1 分箱后，PC2 分布是**多峰**的（双峰/多峰）
  P2) **一维流形**：数据落成一条曲线/线段，密度不均
      → 表现：PC1 分箱后，PC2 的条件标准差**很小**且平滑变化

判据：
  · 若给定 PC1 后 PC2 的条件 std 远小于 PC2 的全局 std → 低维流形
  · 若条件分布多峰 → 真团块
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
print("一、PC1 与 PC2 的关系（正交性检查）")
print("=" * 94)
print(f"  corr(PC1, PC2) = {np.corrcoef(P[:,0], P[:,1])[0,1]:+.6f}  "
      f"（PCA 保证线性无关，必然≈0）")
print(f"  PC1 std={P[:,0].std():.4f}  PC2 std={P[:,1].std():.4f}")

print()
print("=" * 94)
print("二、关键判据：给定 PC1，PC2 的条件分布")
print("=" * 94)
print(f"  {'PC1区间':>18} {'n':>7} {'PC2均值':>9} {'PC2条件std':>11} {'PC2偏度':>9}")
qs = np.percentile(P[:,0], np.linspace(0, 100, 13))
cond_stds = []
for i in range(len(qs)-1):
    m = (P[:,0] >= qs[i]) & (P[:,0] < qs[i+1])
    if m.sum() < 50: continue
    v = P[m,1]
    cond_stds.append(v.std())
    from scipy.stats import skew
    print(f"  [{qs[i]:7.2f},{qs[i+1]:7.2f}) {m.sum():>7d} {v.mean():>9.3f} "
          f"{v.std():>11.3f} {skew(v):>9.3f}")
print(f"\n  PC2 全局 std = {P[:,1].std():.4f}")
print(f"  PC2 条件 std 均值 = {np.mean(cond_stds):.4f}")
print(f"  比值 = {np.mean(cond_stds)/P[:,1].std():.4f}")
print(f"  → 若比值明显 <1（如 <0.7），说明数据落成低维流形，不是填满二维")

print()
print("=" * 94)
print("三、条件分布是否多峰（真团块的判据）")
print("=" * 94)
# 取中间的 PC1 区间，看 PC2 的直方图
mid = (P[:,0] > np.percentile(P[:,0], 40)) & (P[:,0] < np.percentile(P[:,0], 60))
v = P[mid, 1]
h, edges = np.histogram(v, bins=30)
# 数局部极大
pk = 0
for i in range(1, len(h)-1):
    if h[i] > h[i-1] and h[i] > h[i+1] and h[i] > h.max()*0.15:
        pk += 1
print(f"  PC1 在 40~60 分位时，PC2 直方图（30 箱）:")
print(f"    {list(h)}")
print(f"    局部峰数 = {pk}")
print(f"    → {'多峰 = 真团块' if pk >= 2 else '单峰 = 连续分布（流形）'}")

print()
print("=" * 94)
print("四、用 num_products 分层后，各层是否彼此分离")
print("=" * 94)
for npv in sorted(df["num_products"].unique()):
    m = df["num_products"].values == npv
    print(f"  num_products={npv}: n={m.sum():6d}  "
          f"PC1 mean={P[m,0].mean():6.2f} std={P[m,0].std():5.2f}  "
          f"PC2 mean={P[m,1].mean():6.2f} std={P[m,1].std():5.2f}")
print("  → 若各层均值差距 >> 层内 std，则 4 层在图上可分辨")

print()
print("=" * 94)
print("五、二维图覆盖的方差 vs 各维占比")
print("=" * 94)
evr = pca.explained_variance_ratio_
print(f"  各主成分: {[round(float(v),4) for v in evr]}")
print(f"  2D 覆盖 = {evr[:2].sum()*100:.2f}%   丢失 = {(1-evr[:2].sum())*100:.2f}%")
# 前 3 个特征贡献了多少个特征的信息
print()
print("  PC1/PC2 主要由 3 个特征主导（载荷绝对值）:")
for i, f in enumerate(CLUSTER_FEATURES):
    v = abs(pca.components_[0,i]) + abs(pca.components_[1,i])
    if v > 0.3:
        print(f"    {f:22s} {v:.3f}")
print("  → 图中横纵轴几乎只反映 balance / num_products / estimated_salary")
print("    其余 8 个特征（含 credit_score / age / tenure / 满意度）在图上看不见")

print()
print("=" * 94)
print("六、结论性对比表")
print("=" * 94)
from sklearn.manifold import TSNE
rng = np.random.default_rng(42)
N = 15000
idx = rng.choice(len(Xs), N, replace=False)
ts = TSNE(n_components=2, random_state=42, perplexity=30, init="pca",
          max_iter=500, n_iter_without_progress=100).fit_transform(Xs[idx])

def stats(P2, tag):
    H, xe, ye = np.histogram2d(P2[:,0], P2[:,1], bins=40)
    nz = H[H>0]
    # 条件 std（对 x 分箱）
    q = np.percentile(P2[:,0], np.linspace(0,100,11))
    cs = [P2[(P2[:,0]>=q[i])&(P2[:,0]<q[i+1]),1].std() for i in range(10)
          if ((P2[:,0]>=q[i])&(P2[:,0]<q[i+1])).sum()>30]
    print(f"  {tag:10s} 非空格子={int((H>0).sum()):5d}/1600  "
          f"最大格={H.max():6.0f}  "
          f"PC2全局std={P2[:,1].std():6.3f}  "
          f"条件std均值={np.mean(cs):6.3f}  比值={np.mean(cs)/P2[:,1].std():.3f}")
    return np.mean(cs)/P2[:,1].std()

r_pca = stats(P[idx][:, :2], "PCA 2D")
r_tsne = stats(ts, "t-SNE 2D")
print()
print(f"  条件std/全局std: PCA={r_pca:.3f}  t-SNE={r_tsne:.3f}")
print(f"  该比值越小 = 越像低维流形（串珠）；越大 = 越铺满平面")
