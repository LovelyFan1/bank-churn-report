"""反解等距格点来自哪个特征。

关键线索（diag44）：
  相邻密度峰在 PC1 上**精确等距**：差分恒为 0.195
  在 PC2 上差分恒为 -0.297
  比值 -0.297/0.195 = -1.523

若某个整数特征 x 的取值变化 1 会在 PCA 空间产生固定位移，
则位移向量 = (PC1 载荷, PC2 载荷) × (1/σ_x) × 步长
   => 比例 PC2/PC1 = 载荷_PC2 / 载荷_PC1

从载荷表看：
  tenure:   PC1=-0.004, PC2=0.016  → 比值 = -4.0   （符号不符）
  num_products: PC1=-0.428, PC2=-0.542 → 比值 = 1.27
  balance_salary_ratio: 0.596, -0.358 → 比值 = -0.60
  estimated_salary: -0.409, 0.621 → 比值 = -1.52  ✅ 接近 -1.523！

所以等距格点很可能来自 **estimated_salary**！但它是"连续"的…
等一下 —— 需要验证它的**取值步长**。CSV 里 salary 保留 2 位小数，
但生成器可能是按某个整数步长生成的。检查其唯一值间距。
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
print("一、各特征的**唯一值与步长**（找出谁在产生等差格点）")
print("=" * 94)
for f in CLUSTER_FEATURES:
    u = np.unique(df[f].values)
    if len(u) <= 15:
        print(f"  {f:22s} 唯一值 {len(u):6d}  {list(np.round(u,2))[:12]}")
    else:
        d = np.diff(u)
        d = d[d > 1e-9]
        print(f"  {f:22s} 唯一值 {len(u):6d}  最小正步长={d.min():.6f}  "
              f"步长中位数={np.median(d):.6f}  "
              f"是否等距={np.allclose(d, d[0], atol=1e-6)}")

print()
print("=" * 94)
print("二、载荷比值 vs 实测位移比值 (-0.297/0.195 = -1.523)")
print("=" * 94)
target = -0.297 / 0.195
print(f"  目标比值 = {target:.4f}\n")
print(f"  {'特征':22s} {'PC1载荷':>9} {'PC2载荷':>9} {'比值':>9} {'与目标差':>10}")
for i, f in enumerate(CLUSTER_FEATURES):
    l1, l2 = pca.components_[0, i], pca.components_[1, i]
    if abs(l1) < 1e-6:
        continue
    ratio = l2 / l1
    print(f"  {f:22s} {l1:>9.4f} {l2:>9.4f} {ratio:>9.4f} {abs(ratio-target):>10.4f}")

print()
print("=" * 94)
print("三、决定性验证：按 estimated_salary 分箱，看每箱的 PC1 均值是否等距")
print("=" * 94)
for f in ["estimated_salary", "balance", "credit_score", "age"]:
    # 用整数分箱
    v = df[f].values
    lo, hi = np.percentile(v, [0.5, 99.5])
    bins = np.linspace(lo, hi, 21)
    idx = np.digitize(v, bins)
    ctr, pc1m = [], []
    for b in range(1, 21):
        m = idx == b
        if m.sum() > 50:
            ctr.append(np.mean(v[m])); pc1m.append(P[m,0].mean())
    if len(pc1m) >= 4:
        d = np.diff(pc1m)
        print(f"  {f:22s} 各箱 PC1 均值的差分: "
              f"{[round(x,4) for x in d[:8]]}")
        print(f"  {'':22s} 差分标准差={np.std(d):.6f}  "
              f"{'✅ 近似等距' if np.std(d)<0.01 else '❌ 不等距'}")

print()
print("=" * 94)
print("四、直接检查：把 estimated_salary 的**唯一排序值**投影，看步长")
print("=" * 94)
# 各特征单独标准化后投影到 PC1 方向的分量
for i, f in enumerate(CLUSTER_FEATURES):
    col = Xs[:, i]
    comp = col * pca.components_[0, i]   # 该特征对 PC1 的贡献
    print(f"  {f:22s} 对 PC1 贡献: std={comp.std():.4f}  "
          f"range=[{comp.min():7.3f},{comp.max():7.3f}]  占比={comp.std()**2:.4f}")

print()
print("=" * 94)
print("五、终极检验：剔除 num_products 后，等距格点是否消失")
print("=" * 94)
for drop in [[], ["num_products"], ["estimated_salary"], ["num_products","estimated_salary"]]:
    keep = [j for j, f in enumerate(CLUSTER_FEATURES) if f not in drop]
    Xk = X[:, keep]
    sck = StandardScaler(); Xk2 = sck.fit_transform(Xk)
    pk = PCA(n_components=3).fit(Xk2)
    Pk = pk.transform(Xk2)
    Hk, xek, yek = np.histogram2d(Pk[:,0], Pk[:,1], bins=60)
    pks = []
    for a in range(1, Hk.shape[0]-1):
        for b in range(1, Hk.shape[1]-1):
            if Hk[a,b] == Hk[a-1:a+2, b-1:b+2].max() and Hk[a,b] > 200:
                pks.append(((xek[a]+xek[a+1])/2, (yek[b]+yek[b+1])/2, Hk[a,b]))
    pks.sort(key=lambda t: -t[2])
    ps = np.array([[x,y] for x,y,_ in pks[:8]])
    label = "无剔除" if not drop else f"剔除 {','.join(drop)}"
    if len(ps) >= 3:
        o = np.argsort(ps[:,0]); ps2 = ps[o]
        d1 = np.diff(ps2[:,0]); d2 = np.diff(ps2[:,1])
        eq = np.std(d1) < 0.02
        print(f"  {label:34s} 峰数={len(pks):3d} max={int(Hk.max()):5d} "
              f"PC1差分std={np.std(d1):.4f} {'✅等距' if eq else ''}")
        print(f"  {'':34s} PC1差分={[round(x,3) for x in d1[:6]]}")
    else:
        print(f"  {label:34s} 峰数={len(pks):3d} max={int(Hk.max()):5d} (峰太少)")
