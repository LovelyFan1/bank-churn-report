"""第十二轮：查清分块 partial_fit 为何系统性漏掉小簇（疑似关键 bug）。只读。"""
import numpy as np, pandas as pd
from app.services.data_loader import prepare_cluster_features, DataLoader
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans, MiniBatchKMeans

db = SessionLocal()
rows = db.query(Customer).all()
df = pd.DataFrame([{
    "id": r.id, "credit_score": r.credit_score, "age": r.age, "tenure": r.tenure,
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

# 全量 KMeans 找到的小簇成员
km = KMeans(n_clusters=5, random_state=42, n_init=10)
lb = km.fit_predict(Xs)
sizes = [(c, (lb == c).sum()) for c in range(5)]
small = min(sizes, key=lambda t: t[1])[0]
mask_small = lb == small
print(f"全量KMeans 小簇 C{small}: {mask_small.sum()} 人")
print(f"  该簇 estimated_salary: min={df[mask_small]['estimated_salary'].min():.2f} "
      f"max={df[mask_small]['estimated_salary'].max():.2f} mean={df[mask_small]['estimated_salary'].mean():.2f}")
print(f"  该簇 balance_salary_ratio: mean={df[mask_small]['balance_salary_ratio'].mean():.1f}")

print("\n  全体 estimated_salary 分位:")
for q in [0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.10, 0.25, 0.50]:
    print(f"    p{q*100:5.1f} = {df['estimated_salary'].quantile(q):12,.2f}")

thr = df[mask_small]["estimated_salary"].max()
print(f"\n  用 salary <= {thr:.2f} 圈定: {(df['estimated_salary'] <= thr).sum()} 人")
print(f"  与全量KMeans小簇的重合: {(mask_small & (df['estimated_salary'] <= thr)).sum()} / {mask_small.sum()}")

print("\n" + "=" * 96)
print("一、winsorize 后，这些低薪客户被改成了什么值？")
print("=" * 96)
from app.services.data_loader import winsorize_cluster_features, CLUSTER_WINSOR_QUANTILE
w = winsorize_cluster_features(df)
lo = df["estimated_salary"].quantile(CLUSTER_WINSOR_QUANTILE)
print(f"  estimated_salary 的 p{CLUSTER_WINSOR_QUANTILE*100} = {lo:,.2f}")
print(f"  原值 <= {lo:,.2f} 的客户数 = {(df['estimated_salary'] <= lo).sum()}")
print(f"  这些客户原 salary 范围: [{df[df['estimated_salary']<=lo]['estimated_salary'].min():.2f}, "
      f"{df[df['estimated_salary']<=lo]['estimated_salary'].max():.2f}]")
print(f"  winsorize 后统一变为: {w['estimated_salary'].min():,.2f}")
print(f"\n  ⚠ 也就是说：**这 {(df['estimated_salary']<=lo).sum()} 个客户的薪资被全部压成同一个值"
      f" {lo:,.2f}**，")
print(f"     他们之间原本的差异（从 {df[df['estimated_salary']<=lo]['estimated_salary'].min():.2f} "
      f"到 {df[df['estimated_salary']<=lo]['estimated_salary'].max():.2f}）被抹平了。")

# 被截断的客户在标准化后是什么值
sc = StandardScaler().fit(Xs)
idx_sal = 7  # estimated_salary 在 CLUSTER_FEATURES 中的位置
print(f"\n  estimated_salary 在 CLUSTER_FEATURES 的索引 = {idx_sal}")
print(f"  截断值 {lo:,.2f} 标准化后 z = {(lo - sc.mean_[idx_sal])/sc.scale_[idx_sal]:.4f}")

print("\n" + "=" * 96)
print("二、关键：MiniBatchKMeans.partial_fit 为什么找不到小簇")
print("=" * 96)
print("""
  机制推断（下面逐条验证）：
    MiniBatchKMeans 的 partial_fit 每次只看 batch_size 条数据来更新质心。
    当前任务参数 batch_size=10000，且**按 id 顺序**分块喂入
    （DataLoader.iter_chunks 用 ORDER BY id + OFFSET）。
    如果这 1251 个低薪客户在 id 顺序上**聚集在某一小段**，
    那么初始的 n_init=3 次初始化很可能压根没覆盖到他们，
    之后质心被大量主流样本持续拉向中心，小簇永远占用不到一个质心。
""")

# 检查低薪客户在 id 顺序上的分布
low = df["estimated_salary"] <= lo
print(f"  验证 A：低薪客户({low.sum()}人)在 id 顺序上的位置")
ids = df.loc[low, "id"].values
print(f"    最小 id={ids.min()}  最大 id={ids.max()}")
for p in [0, 10, 25, 50, 75, 90, 100]:
    print(f"    id 的 p{p:3d} = {np.percentile(ids, p):,.0f}")
n = len(df)
first_third = (ids < n/3).mean() * 100
mid_third = ((ids >= n/3) & (ids < 2*n/3)).mean() * 100
last_third = (ids >= 2*n/3).mean() * 100
print(f"    落在 [0,33%) 区间: {first_third:.1f}%   [33%,67%): {mid_third:.1f}%   [67%,100%]: {last_third:.1f}%")
print(f"    → 若三个区间占比接近 33%，说明散落分布；若集中，说明聚集")

print(f"\n  验证 B：把 batch_size 调大后能否找到小簇")
db2 = SessionLocal(); loader = DataLoader(db2)
cs = loader.chunksize
offs = list(range(0, loader.total_count, cs))[:3]
sca = StandardScaler().fit(np.vstack([prepare_cluster_features(loader.load_chunk(o)) for o in offs]))
scaled_all = sca.transform(X)

for bs, ni in [(10000, 3), (10000, 10), (50000, 3), (100000, 3)]:
    mb = MiniBatchKMeans(n_clusters=5, random_state=42, batch_size=bs, n_init=ni)
    for ch in loader.iter_chunks():
        mb.partial_fit(sca.transform(prepare_cluster_features(ch)))
    l = np.concatenate([mb.predict(sca.transform(prepare_cluster_features(ch)))
                        for ch in loader.iter_chunks()])
    sz = sorted([(l == c).sum() for c in range(5)], reverse=True)
    hit_small = 0
    # 哪个簇与真实低薪组重合最多
    for c in range(5):
        ov = ((l == c) & low.values).sum()
        hit_small = max(hit_small, ov)
    print(f"    batch_size={bs:6d} n_init={ni:2d}: 簇大小={sz}  "
          f"最大簇含低薪客户={hit_small}/{low.sum()}")
db2.close()

print(f"\n  验证 C：一次性 fit（不分块）能否找到")
mb2 = MiniBatchKMeans(n_clusters=5, random_state=42, batch_size=10000, n_init=3)
l2 = mb2.fit_predict(scaled_all)
sz2 = sorted([(l2 == c).sum() for c in range(5)], reverse=True)
ov2 = max(((l2 == c) & low.values).sum() for c in range(5))
print(f"    一次性 fit_predict: 簇大小={sz2}  最大簇含低薪客户={ov2}/{low.sum()}")

print(f"\n  验证 D：KMeans 全量（非 MiniBatch）")
km3 = KMeans(n_clusters=5, random_state=42, n_init=10)
l3 = km3.fit_predict(scaled_all)
sz3 = sorted([(l3 == c).sum() for c in range(5)], reverse=True)
ov3 = max(((l3 == c) & low.values).sum() for c in range(5))
print(f"    KMeans: 簇大小={sz3}  最大簇含低薪客户={ov3}/{low.sum()}")
