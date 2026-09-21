"""第十一轮：调查 (a) 稳定的 1250 人小簇是什么 (b) DB 结果与标准 KMeans 的差异。只读。"""
import numpy as np, pandas as pd
from app.services.data_loader import prepare_cluster_features, CLUSTER_FEATURES
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans, MiniBatchKMeans
from sklearn.metrics import silhouette_score

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

print("=" * 100)
print("一、全量 KMeans k=5 中那个 1251 人的小簇，到底是什么客户？")
print("=" * 100)
km = KMeans(n_clusters=5, random_state=42, n_init=10)
lb = km.fit_predict(Xs)
sizes = [(c, (lb == c).sum()) for c in range(5)]
sizes.sort(key=lambda t: t[1])
small = sizes[0][0]
big = sizes[-1][0]
print(f"  簇大小: {[f'C{c}={n}' for c, n in sizes]}")
print(f"  → 最小簇 C{small} ({sizes[0][1]} 人)\n")

show = ["credit_score","age","tenure","balance","num_products","has_credit_card",
        "is_active_member","estimated_salary","satisfaction_score","points_earned",
        "balance_salary_ratio"]
m = pd.DataFrame({
    f"小簇C{small}": df[lb == small][show].mean(),
    f"最大簇C{big}": df[lb == big][show].mean(),
    "全体": df[show].mean(),
}).round(2)
m["小簇/全体倍数"] = (m[f"小簇C{small}"] / m["全体"].replace(0, np.nan)).round(2)
print(m.to_string())

print(f"\n  小簇流失率 = {y[lb==small].mean()*100:.2f}%  (全体 {y.mean()*100:.2f}%)")
print(f"  小簇 balance==0 占比 = {(df[lb==small]['balance']==0).mean()*100:.1f}%")
print(f"  全体  balance==0 占比 = {(df['balance']==0).mean()*100:.1f}%")
print(f"  小簇 num_products 分布 = {df[lb==small]['num_products'].value_counts().sort_index().to_dict()}")
print(f"  全体 num_products 分布 = {df['num_products'].value_counts().sort_index().to_dict()}")

print()
print("=" * 100)
print("二、这个 1250 人小簇在 k=4/5/6/8 下是否稳定存在")
print("=" * 100)
for k in [3, 4, 5, 6, 7, 8]:
    km2 = KMeans(n_clusters=k, random_state=42, n_init=10)
    l2 = km2.fit_predict(Xs)
    sz = sorted([(l2 == c).sum() for c in range(k)])
    tiny = [n for n in sz if n < 3000]
    print(f"  k={k}: 最小3个簇 = {sz[:3]}   {'⚠ 存在<3000的小簇' if tiny else '无小簇'}")

print()
print("=" * 100)
print("三、DB 中保存的结果 vs 标准 KMeans —— 差异有多大")
print("=" * 100)
db_lab = df["cluster_id"].values.astype(int)
print(f"  DB 簇大小      : {sorted([(db_lab==c).sum() for c in sorted(set(db_lab))], reverse=True)}")
print(f"  标准KMeans k=5 : {sorted([(lb==c).sum() for c in range(5)], reverse=True)}")
print(f"\n  DB 各簇流失率  : {[round(y[db_lab==c].mean()*100,1) for c in sorted(set(db_lab))]}")
print(f"  标准各簇流失率 : {[round(y[lb==c].mean()*100,1) for c in range(5)]}")

sil_db = silhouette_score(Xs, db_lab, sample_size=20000, random_state=42)
sil_km = silhouette_score(Xs, lb, sample_size=20000, random_state=42)
print(f"\n  DB 结果的 silhouette      = {sil_db:+.4f}")
print(f"  标准 KMeans 的 silhouette = {sil_km:+.4f}")
print(f"  DB inertia  = {sum(((Xs[db_lab==c] - Xs[db_lab==c].mean(0))**2).sum() for c in set(db_lab)):,.0f}")
print(f"  标准 inertia = {km.inertia_:,.0f}")

# 复现 DB 的训练方式（分块 partial_fit）
from app.services.data_loader import DataLoader
from app.config import settings
db2 = SessionLocal()
loader = DataLoader(db2)
cs = loader.chunksize
offs = list(range(0, loader.total_count, cs))[:3]
sc = StandardScaler()
sc.fit(np.vstack([prepare_cluster_features(loader.load_chunk(o)) for o in offs]))
mb = MiniBatchKMeans(n_clusters=5, random_state=settings.RANDOM_STATE,
                     batch_size=10000, n_init=3)
for ch in loader.iter_chunks():
    mb.partial_fit(sc.transform(prepare_cluster_features(ch)))
lb_mb = np.concatenate([mb.predict(sc.transform(prepare_cluster_features(ch)))
                        for ch in loader.iter_chunks()])
print(f"\n  ── 复现分块 partial_fit（当前任务的做法）──")
print(f"  簇大小     : {sorted([(lb_mb==c).sum() for c in range(5)], reverse=True)}")
print(f"  各簇流失率 : {[round(y[lb_mb==c].mean()*100,1) for c in range(5)]}")
print(f"  silhouette = {silhouette_score(Xs, lb_mb, sample_size=20000, random_state=42):+.4f}")
print(f"  与 DB 标签一致率 = {(lb_mb == db_lab).mean()*100:.2f}%")
db2.close()

print()
print("=" * 100)
print("四、若把 n_init 提到 10，分块 partial_fit 能否也找到小簇")
print("=" * 100)
for ni in [3, 10, 30]:
    db3 = SessionLocal(); l3 = DataLoader(db3)
    mb3 = MiniBatchKMeans(n_clusters=5, random_state=42, batch_size=10000, n_init=ni)
    for ch in l3.iter_chunks():
        mb3.partial_fit(sc.transform(prepare_cluster_features(ch)))
    l3b = np.concatenate([mb3.predict(sc.transform(prepare_cluster_features(ch)))
                          for ch in l3.iter_chunks()])
    print(f"  n_init={ni:2d}: 簇大小={sorted([(l3b==c).sum() for c in range(5)], reverse=True)}  "
          f"流失率={[round(y[l3b==c].mean()*100,1) for c in range(5)]}")
    db3.close()
