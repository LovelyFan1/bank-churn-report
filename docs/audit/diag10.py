"""第十轮：稳健性检验 —— silhouette 对抽样/种子的敏感性 + n_init 影响。

⚠ 触发本脚本的原因：diag6（3万抽样）得出 k=3 silhouette 最优(0.0949)，
   而 diag9（5万抽样）得出 k=2 最优(0.0962)、k=3 只有 0.0678。
   **同一指标在同数据上排名翻转**，说明我上一轮"k=3 更优"的结论不稳健。
   必须查清：是抽样噪声，还是 k 之间本就差异极小。
"""
import numpy as np, pandas as pd
from app.services.data_loader import prepare_cluster_features
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import MiniBatchKMeans, KMeans
from sklearn.metrics import silhouette_score, adjusted_rand_score

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
Xs = StandardScaler().fit_transform(X)
y = df["exited"].values

print("=" * 96)
print("一、silhouette 对**抽样规模**的敏感性（每个规模重复 5 次不同抽样）")
print("=" * 96)
K = [2, 3, 4, 5, 6]
print(f"{'k':>3} " + "".join(f"{f'n={n//1000}k':>26}" for n in [10000, 30000, 50000, 100000]))
rows_out = []
for k in K:
    cells = []
    for n in [10000, 30000, 50000, 100000]:
        vals = []
        for rep in range(5):
            rng = np.random.default_rng(rep * 100 + k)
            idx = rng.choice(len(Xs), n, replace=False)
            Xc = Xs[idx]
            km = MiniBatchKMeans(n_clusters=k, random_state=42, batch_size=10000, n_init=10)
            lb = km.fit_predict(Xc)
            ss = min(10000, n)
            vals.append(silhouette_score(Xc, lb, sample_size=ss, random_state=42))
        cells.append(f"{np.mean(vals):+.4f}±{np.std(vals):.4f}")
    print(f"{k:>3} " + "".join(f"{c:>26}" for c in cells))
    rows_out.append((k, cells))

print("\n  ⚠ 读数方式：'均值±标准差' 是**5 次不同随机抽样**之间的波动。")
print("     若同一 k 在不同抽样规模下排名都变，说明 k 之间差异小于抽样噪声。")

print()
print("=" * 96)
print("二、各 k 在**同一抽样**下的差值是否超出抽样噪声")
print("=" * 96)
n = 50000
all_vals = {}
for k in K:
    vals = []
    for rep in range(10):
        rng = np.random.default_rng(rep * 7 + k)
        idx = rng.choice(len(Xs), n, replace=False)
        km = MiniBatchKMeans(n_clusters=k, random_state=42, batch_size=10000, n_init=10)
        lb = km.fit_predict(Xs[idx])
        vals.append(silhouette_score(Xs[idx], lb, sample_size=10000, random_state=42))
    all_vals[k] = vals
    print(f"  k={k}: mean={np.mean(vals):+.4f}  std={np.std(vals):.4f}  "
          f"range=[{min(vals):+.4f}, {max(vals):+.4f}]")

mn = {k: np.mean(v) for k, v in all_vals.items()}
best = max(mn, key=mn.get)
noise = np.mean([np.std(v) for v in all_vals.values()])
print(f"\n  最优 k = {best} (mean silhouette={mn[best]:+.4f})")
print(f"  抽样噪声（各 k 标准差的均值）= {noise:.4f}")
for k in K:
    gap = mn[best] - mn[k]
    verdict = "超出噪声" if gap > noise else "**在噪声范围内**"
    print(f"    k={best} vs k={k}: 差 {gap:+.4f}  {verdict}")

print()
print("=" * 96)
print("三、n_init 的影响（当前 DB 用的是 n_init=3，偏小）")
print("=" * 96)
print("  ⚠ 当前 Celery 任务 `_choose_kmeans` 里 MiniBatchKMeans 用 n_init=3、")
print("     batch_size=10000。n_init 越小，越容易停在较差的局部最优。\n")
idx = np.random.default_rng(42).choice(len(Xs), 50000, replace=False)
Xc, yc = Xs[idx], y[idx]
for k in [4, 5, 6]:
    print(f"  ── k={k} ──")
    for ni in [1, 3, 10, 30]:
        rates, aris = [], []
        labs = []
        for sd in range(5):
            km = MiniBatchKMeans(n_clusters=k, random_state=sd, batch_size=10000, n_init=ni)
            lb = km.fit_predict(Xc)
            labs.append(lb)
            rates.append(max(yc[lb == c].mean() for c in range(k)) * 100
                         - min(yc[lb == c].mean() for c in range(k)) * 100)
        for i in range(len(labs)):
            for j in range(i + 1, len(labs)):
                aris.append(adjusted_rand_score(labs[i], labs[j]))
        print(f"     n_init={ni:2d}: churn极差={np.mean(rates):5.2f}pp  跨种子ARI={np.mean(aris):+.4f}")

print()
print("=" * 96)
print("四、完整数据（无抽样）下的最终对照：k=4/5/6")
print("=" * 96)
print("  用全量 10 万行做 KMeans（非 MiniBatch），n_init=10，看真实指标\n")
for k in [4, 5, 6]:
    km = KMeans(n_clusters=k, random_state=42, n_init=10)
    lb = km.fit_predict(Xs)
    rates = [y[lb == c].mean() * 100 for c in range(k)]
    sizes = [(lb == c).sum() for c in range(k)]
    sil = silhouette_score(Xs, lb, sample_size=20000, random_state=42)
    print(f"  k={k}: silhouette={sil:+.4f}  inertia={km.inertia_:,.0f}")
    print(f"        各簇人数={sizes}")
    print(f"        各簇流失率={[round(r,1) for r in rates]}  极差={max(rates)-min(rates):.2f}pp")
    print()
