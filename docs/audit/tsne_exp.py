"""t-SNE 方案实验：确定参数、耗时与效果。只读。

需要确定：
  1) 在多少点上跑（散点图上限 MAX_SCATTER_POINTS=15000）
  2) perplexity 取值对结构与耗时的影响
  3) 单次耗时是否在 Celery 软超时（1800s）内留足余量
  4) 与 PCA 对比：密度峰、最大格、5-NN 可分性
"""
import sys, time
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
from sklearn.manifold import TSNE
from sklearn.metrics import silhouette_score
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import cross_val_score

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
lab = df["cluster_id"].values.astype(int)

print("=" * 96)
print("一、t-SNE 耗时 vs 样本量（perplexity=30）")
print("=" * 96)
print(f"  {'n':>7} {'耗时(s)':>9} {'峰值内存增量':>12}")
for n in [5000, 10000, 15000, 20000]:
    rng = np.random.default_rng(42)
    idx = rng.choice(len(Xs), n, replace=False)
    t0 = time.time()
    ts = TSNE(n_components=2, random_state=42, perplexity=30, init="pca",
              max_iter=500, n_iter_without_progress=100).fit_transform(Xs[idx])
    dt = time.time() - t0
    print(f"  {n:>7} {dt:>9.1f}")
    del ts

print()
print("=" * 96)
print("二、perplexity 的影响（n=15000）")
print("=" * 96)
rng = np.random.default_rng(42)
N = 15000
idx = rng.choice(len(Xs), N, replace=False)
Xs_s, lab_s = Xs[idx], lab[idx]

def density_stats(P2, tag):
    H, xe, ye = np.histogram2d(P2[:,0], P2[:,1], bins=40)
    nz = H[H > 0]
    peaks = 0
    for i in range(1, 39):
        for j in range(1, 39):
            if H[i,j] == H[i-1:i+2, j-1:j+2].max() and H[i,j] > N/1600*4:
                peaks += 1
    q = np.percentile(P2[:,0], np.linspace(0, 100, 11))
    cs = [P2[(P2[:,0]>=q[i])&(P2[:,0]<q[i+1]),1].std() for i in range(10)
          if ((P2[:,0]>=q[i])&(P2[:,0]<q[i+1])).sum() > 30]
    return dict(tag=tag, nonempty=int((H>0).sum()), maxcell=int(H.max()),
                peaks=peaks, ratio=float(np.mean(cs)/P2[:,1].std()))

res = []
for perp in [15, 30, 50]:
    t0 = time.time()
    ts = TSNE(n_components=2, random_state=42, perplexity=perp, init="pca",
              max_iter=500, n_iter_without_progress=100).fit_transform(Xs_s)
    dt = time.time() - t0
    st = density_stats(ts, f"t-SNE perp={perp}")
    st["time"] = dt
    # 5-NN 可分性
    st["5nn"] = float(cross_val_score(KNeighborsClassifier(5), ts, lab_s, cv=3).mean())
    res.append(st)
    print(f"  perplexity={perp:3d}: {dt:6.1f}s  非空格子={st['nonempty']:4d} "
          f"最大格={st['maxcell']:4d}  峰={st['peaks']:3d}  "
          f"条件std比={st['ratio']:.3f}  5-NN={st['5nn']*100:.1f}%")

# PCA 对照
pca = PCA(n_components=3).fit(Xs_s)
Pp = pca.transform(Xs_s)
st = density_stats(Pp[:, :2], "PCA")
st["time"] = 0.0
st["5nn"] = float(cross_val_score(KNeighborsClassifier(5), Pp[:, :2], lab_s, cv=3).mean())
res.append(st)
print(f"  {'PCA':>15s}: {0.0:6.1f}s  非空格子={st['nonempty']:4d} "
      f"最大格={st['maxcell']:4d}  峰={st['peaks']:3d}  "
      f"条件std比={st['ratio']:.3f}  5-NN={st['5nn']*100:.1f}%")

print()
print("=" * 96)
print("三、可复现性检验（同 random_state 两次结果是否一致）")
print("=" * 96)
t1 = TSNE(n_components=2, random_state=42, perplexity=30, init="pca",
          max_iter=500).fit_transform(Xs_s[:8000])
t2 = TSNE(n_components=2, random_state=42, perplexity=30, init="pca",
          max_iter=500).fit_transform(Xs_s[:8000])
print(f"  两次最大坐标差 = {np.abs(t1-t2).max():.6e}")
print(f"  → {'✅ 完全可复现' if np.abs(t1-t2).max() < 1e-9 else '⚠ 有随机差异'}")

print()
print("=" * 96)
print("四、方案对照表")
print("=" * 96)
print(f"  {'方案':<20} {'非空格子':>9} {'最大格':>8} {'密度峰':>7} {'5-NN':>8} {'耗时':>8}")
for r in res:
    nm = r["tag"]
    print(f"  {nm:<20} {r['nonempty']:>9} {r['maxcell']:>8} {r['peaks']:>7} "
          f"{r['5nn']*100:>7.1f}% {r['time']:>7.1f}s")
print()
print("  说明：非空格子越多 / 最大格越小 / 密度峰越少 → 结构越摊开")
print("        5-NN 越高 → 簇在图上越可分（注意 t-SNE 上这是乐观估计）")
