"""第二轮诊断：分块 winsorize 一致性 + 聚类可复现性。只读。"""
import numpy as np
import pandas as pd
from app.services.data_loader import (
    prepare_cluster_features, CLUSTER_FEATURES, winsorize_cluster_features,
    DataLoader, CLUSTER_WINSOR_QUANTILE,
)
from app.database import SessionLocal
from app.models.customer import Customer
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import silhouette_score

db = SessionLocal()
loader = DataLoader(db)
total = loader.total_count
cs = loader.chunksize
print(f"total={total} chunksize={cs}")

# ── 1. 分块 winsorize 的阈值差异 ──────────────────────
print("\n=== 1. 各 chunk 独立 winsorize 的截断阈值差异 ===")
print(f"（CLUSTER_WINSOR_QUANTILE={CLUSTER_WINSOR_QUANTILE}）")
chunks = list(loader.iter_chunks())
print(f"chunk 数: {len(chunks)}, 各 chunk 行数: {[len(c) for c in chunks]}")

for col in ["balance_salary_ratio", "balance", "estimated_salary"]:
    print(f"\n  [{col}]")
    his = []
    for i, ch in enumerate(chunks):
        w = winsorize_cluster_features(ch)
        hi = w[col].max()
        his.append(hi)
        print(f"    chunk{i}: 截断后 max = {hi:.4f}")
    if len(his) > 1:
        spread = max(his) - min(his)
        print(f"    → 跨 chunk 极差 = {spread:.4f}", end="")
        print("  ⚠ 不一致" if spread > 1e-9 else "  一致")

# 全量 winsorize 作为参照
rows = db.query(Customer).all()
df_all = pd.DataFrame([{
    "id": r.id, "credit_score": r.credit_score, "age": r.age, "tenure": r.tenure,
    "balance": r.balance, "num_products": r.num_products,
    "has_credit_card": r.has_credit_card, "is_active_member": r.is_active_member,
    "estimated_salary": r.estimated_salary, "exited": r.exited,
    "satisfaction_score": r.satisfaction_score, "points_earned": r.points_earned,
    "balance_salary_ratio": r.balance_salary_ratio, "cluster_id": r.cluster_id,
} for r in rows])
w_all = winsorize_cluster_features(df_all)
print(f"\n  全量 winsorize: balance_salary_ratio max = {w_all['balance_salary_ratio'].max():.4f}")

# ── 2. 复刻任务流程，看能否复现 DB 标签 ────────────────
print("\n=== 2. 复刻 Celery 任务流程（scaler 用前3 chunk fit → 分块 partial_fit）===")
from app.config import settings
sample_offsets = list(range(0, total, cs))[:3]
scaler = StandardScaler()
scaler.fit(np.vstack([prepare_cluster_features(loader.load_chunk(o)) for o in sample_offsets]))

kmeans = MiniBatchKMeans(n_clusters=5, random_state=settings.RANDOM_STATE,
                         batch_size=10000, n_init=3)
for ch in loader.iter_chunks():
    kmeans.partial_fit(scaler.transform(prepare_cluster_features(ch)))

# 预测
all_labels = []
for ch in loader.iter_chunks():
    all_labels.append(kmeans.predict(scaler.transform(prepare_cluster_features(ch))))
labels = np.concatenate(all_labels)
print("  各簇大小:", {int(k): int(v) for k, v in zip(*np.unique(labels, return_counts=True))})
print("  DB 中保存的:", {int(k): int(v) for k, v in
      zip(*np.unique(df_all['cluster_id'].dropna().values, return_counts=True))})

ct = pd.crosstab(df_all["cluster_id"].values, labels)
print("\n  交叉表 (行=DB 已保存, 列=本次复刻):")
print(ct.to_string())
# 用 Hungarian 算法找最佳匹配，判断是否只是标签编号不同
from scipy.optimize import linear_sum_assignment
cost = -ct.values
ri, ci = linear_sum_assignment(cost)
mapping = {int(r): int(c) for r, c in zip(ri, ci)}
print(f"\n  最佳标签映射: {mapping}")
mapped = np.array([mapping[int(x)] for x in df_all["cluster_id"].values])
agree = (mapped == labels).mean()
print(f"  映射后一致率 = {agree*100:.2f}%")
print("  → 若远低于 90%，说明聚类结果**不可复现**")

# ── 3. 决定性检验：同一数据重复跑两次，结果是否一致 ─────
print("\n=== 3. 同一流程跑两遍（仅换随机种子外的因素）===")
def run_once(seed):
    km = MiniBatchKMeans(n_clusters=5, random_state=seed, batch_size=10000, n_init=3)
    for ch in loader.iter_chunks():
        km.partial_fit(scaler.transform(prepare_cluster_features(ch)))
    lb = np.concatenate([km.predict(scaler.transform(prepare_cluster_features(ch)))
                         for ch in loader.iter_chunks()])
    return lb
l1 = run_once(settings.RANDOM_STATE)
l2 = run_once(settings.RANDOM_STATE)
print(f"  同种子两次一致率 = {(l1 == l2).mean()*100:.2f}%")

# ── 4. 2D 投影 vs 各簇真实可分性 ────────────────────
print("\n=== 4. 分块流程下的 silhouette（11D vs 2D）===")
rows_s = df_all.iloc[::10]
X_s = scaler.transform(prepare_cluster_features(rows_s))
lbl_s = labels[::10]
print(f"  silhouette 11D = {silhouette_score(X_s, lbl_s):.4f}")
from sklearn.decomposition import PCA
X_s_pca = PCA(n_components=3).fit_transform(X_s)
print(f"  silhouette 2D (PC1-PC2) = {silhouette_score(X_s_pca[:, :2], lbl_s):.4f}")
print(f"  silhouette 3D (PC1-3)   = {silhouette_score(X_s_pca, lbl_s):.4f}")

db.close()
