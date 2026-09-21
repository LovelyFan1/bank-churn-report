"""量化 t-SNE 上线后的改善（对比 PCA）。只读。

从**接口返回的真实数据**计算，而不是重新拟合 —— 确保测的就是页面上的图。
"""
import json, urllib.request
import numpy as np

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=240) as r:
        return json.loads(r.read().decode())

print("=" * 94)
print("一、接口当前返回的方法")
print("=" * 94)
d = get("/api/cluster/3d-scatter")
method = d.get("method")
print(f"  method = {method}")
print(f"  total={d['total_points']} plotted={d['plotted_points']} sampled={d['sampled']}")
print(f"  explained_variance = {d.get('explained_variance')}")

# 组装点（含簇标签）
pts = []
labs = []
for g in d["data"]:
    for p in g["points"]:
        pts.append([p[0], p[1]])
        labs.append(g["cluster_id"])
P = np.array(pts); L = np.array(labs)
print(f"  点数 = {len(P)}  x范围=[{P[:,0].min():.1f},{P[:,0].max():.1f}]  "
      f"y范围=[{P[:,1].min():.1f},{P[:,1].max():.1f}]")

def stats(P2, L2, tag):
    H, _, _ = np.histogram2d(P2[:,0], P2[:,1], bins=40)
    nz = H[H > 0]
    peaks = 0
    for i in range(1, 39):
        for j in range(1, 39):
            if H[i,j] == H[i-1:i+2, j-1:j+2].max() and H[i,j] > len(P2)/1600*4:
                peaks += 1
    q = np.percentile(P2[:,0], np.linspace(0, 100, 11))
    cs = [P2[(P2[:,0]>=q[i])&(P2[:,0]<q[i+1]),1].std() for i in range(10)
          if ((P2[:,0]>=q[i])&(P2[:,0]<q[i+1])).sum() > 30]
    # 簇间最近邻纯度：每个点的 5 邻居里同簇比例（用简单的网格近似太粗，这里用 KDTree）
    from scipy.spatial import cKDTree
    t = cKDTree(P2)
    _, idx = t.query(P2, k=6)
    same = np.mean([np.mean(L2[idx[i][1:]] == L2[i]) for i in range(0, len(P2), 7)])
    return dict(tag=tag, nonempty=int((H>0).sum()), maxcell=int(H.max()),
                peaks=peaks, cond=float(np.mean(cs)/P2[:,1].std()), purity=float(same))

s = stats(P, L, method)
print()
print("=" * 94)
print("二、当前指标的量化")
print("=" * 94)
print(f"  非空格子        = {s['nonempty']}/1600  （越多越铺开）")
print(f"  最大格子人数    = {s['maxcell']}  （越小越均匀）")
print(f"  密度峰个数      = {s['peaks']}  （越少越平滑）")
print(f"  条件std/全局std = {s['cond']:.3f}  （越接近1越铺满平面）")
print(f"  5-邻居同簇比例  = {s['purity']*100:.1f}%  （越高说明簇越凝聚）")

print()
print("=" * 94)
print("三、与 PCA 的对照（参考值来自改造前实测）")
print("=" * 94)
ref = {"nonempty": 454, "maxcell": 293, "peaks": 10, "cond": 0.850, "purity": None}
print(f"  {'指标':<20} {'PCA(改造前)':>14} {'当前(t-SNE)':>14} {'改善':>12}")
for k, label in [("nonempty","非空格子"), ("maxcell","最大格子人数"),
                 ("peaks","密度峰个数"), ("cond","条件std/全局std")]:
    a, b = ref[k], s[k]
    if k == "maxcell":
        chg = f"↓ {(1-b/a)*100:.0f}%"
    elif k == "peaks":
        chg = f"↓ {(1-b/a)*100:.0f}%"
    elif k == "nonempty":
        chg = f"↑ {(b/a-1)*100:.0f}%"
    else:
        chg = f"↑ {(b/a-1)*100:.0f}%"
    print(f"  {label:<20} {a:>14} {b:>14} {chg:>12}")
print(f"  {'5-邻居同簇比例':<20} {'44.2%(PCA 5-NN)':>14} {s['purity']*100:>13.1f}%")

print()
print("=" * 94)
print("四、各簇在 t-SNE 上的分离情况")
print("=" * 94)
for c in sorted(set(L)):
    m = L == c
    cx, cy = P[m,0].mean(), P[m,1].mean()
    print(f"  C{c}: n={m.sum():5d}  中心=({cx:7.2f},{cy:7.2f})  "
          f"x范围=[{P[m,0].min():7.1f},{P[m,0].max():7.1f}]  "
          f"y范围=[{P[m,1].min():7.1f},{P[m,1].max():7.1f}]")

# 簇间中心距离 vs 簇内离散度
cs_ = sorted(set(L))
centers = np.array([[P[L==c,0].mean(), P[L==c,1].mean()] for c in cs_])
instd = np.mean([np.hypot(P[L==c,0].std(), P[L==c,1].std()) for c in cs_])
from itertools import combinations
dists = [np.hypot(*(centers[i]-centers[j])) for i, j in combinations(range(len(cs_)), 2)]
print()
print(f"  簇间中心距离（均值） = {np.mean(dists):.2f}")
print(f"  簇内离散度（均值）   = {instd:.2f}")
print(f"  分离比 = {np.mean(dists)/instd:.2f}")
print(f"  （PCA 时代的对比：层间 1.243 / 层内 1.345 = 0.92，即重叠）")
print(f"  → {'✅ 簇已彼此分开' if np.mean(dists)/instd > 1.2 else '⚠ 仍有重叠'}")
