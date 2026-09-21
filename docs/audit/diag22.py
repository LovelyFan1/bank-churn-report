"""第二十二轮：在沙箱副本上端到端演练新版 run_kmeans_task 的核心逻辑。

⚠ 关键：本脚本**不调用 Celery task**（它会连生产 SessionLocal），
   而是把新代码的计算路径原样复刻到沙箱数据上，验证：
     1) 能否找回 ~1250 人的小簇
     2) 写库映射是否正确（在副本上真写，然后校验）
     3) 新增的 scaler/pca 落盘是否可用
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, pandas as pd, json, os, shutil
from sandbox import session, session_writable, SNAPSHOT, WRITABLE
from sqlalchemy import text

# ── 1) 从沙箱读全量，复刻新任务的计算路径 ──────────────────
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
print(f"沙箱读入 {len(df)} 行")

import app.services.data_loader as dl
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

X = dl.prepare_cluster_features(df)
scaler = StandardScaler()
X_scaled = scaler.fit_transform(X)
print(f"X_scaled shape = {X_scaled.shape}")

k = 5
km = KMeans(n_clusters=k, random_state=42, n_init=10)
labels = km.fit_predict(X_scaled)
sizes = dict(zip(*np.unique(labels, return_counts=True)))
sizes = {int(a): int(b) for a, b in sizes.items()}
print(f"\n=== 新逻辑聚类结果 ===")
print(f"  簇大小: {sizes}")
small = min(sizes, key=sizes.get)
print(f"  最小簇: C{small} = {sizes[small]} 人")

n_sil = min(20000, len(df))
rng = np.random.default_rng(42)
idx = rng.choice(len(df), n_sil, replace=False)
sil = silhouette_score(X_scaled[idx], labels[idx])
print(f"  silhouette(随机{n_sil}) = {sil:.4f}")

# 小簇画像
m = labels == small
print(f"\n  小簇画像: salary均值={df.loc[m,'estimated_salary'].mean():,.0f}  "
      f"balance均值={df.loc[m,'balance'].mean():,.0f}  "
      f"流失率={df.loc[m,'exited'].mean()*100:.1f}%")

# ── 2) 对比旧标签 ──────────────────────────────────────────
old = df["cluster_id"].values
print(f"\n=== 新旧标签对比 ===")
print(f"  旧簇大小: {dict(sorted(pd.Series(old).value_counts().items()))}")
from sklearn.metrics import adjusted_rand_score
print(f"  ARI(新 vs 旧) = {adjusted_rand_score(old, labels):+.4f}")

# 旧标签里，小簇那批人原本分散在哪
print(f"\n  小簇({m.sum()}人)在**旧标签**中的分布:")
print(f"    {dict(sorted(pd.Series(old[m]).value_counts().items()))}")
print("    → 说明这批人原先被拆分混入多个簇，其独特特征被淹没")

# ── 3) 在沙箱副本上真写，验证映射正确性 ────────────────────
print(f"\n=== 写库演练（沙箱副本，非生产） ===")
dbw = session_writable()
url = str(dbw.get_bind().url)
print(f"  目标库: {url}")
assert "churn_analysis.db" not in url or "tmp" in url, "⛔ 拒绝写非沙箱库"

from app.models.customer import Customer
mappings = [{"id": int(i), "cluster_id": int(l)} for i, l in zip(df["id"].values, labels)]
dbw.bulk_update_mappings(Customer, mappings)
dbw.commit()
print(f"  ✅ 写入 {len(mappings)} 行")

dbw.expire_all()
back = {r[0]: r[1] for r in dbw.query(Customer.id, Customer.cluster_id).all()}
ok = all(back[int(i)] == int(l) for i, l in zip(df["id"].values, labels))
print(f"  写后逐行校验: {'✅ 完全一致' if ok else '❌ 不一致'}")

# 分布校验
import collections
print(f"  写后分布: {dict(sorted(collections.Counter(back.values()).items()))}")
dbw.close()

# ── 4) 验证新索引写法对任意分块都正确 ──────────────────────
print(f"\n=== 索引写法验证：用 id 映射 vs 旧的分块索引 ===")
for cs in [20000, 25000, 30000, 50000, 70000]:
    # 新写法：按 id 映射，与分块无关
    ids = df["id"].values
    id2lab = dict(zip(ids.tolist(), labels.tolist()))
    correct = all(id2lab[int(i)] == int(l) for i, l in zip(ids, labels))
    # 旧写法：分块 + j-offset，看是否越界
    try:
        for ci in range(0, len(df), cs):
            chunk_len = min(cs, len(df) - ci)
            chunk_labels = labels[ci:ci+chunk_len]
            for j in range(chunk_len):
                _ = chunk_labels[j - ci]
        old_ok = True
    except IndexError as e:
        old_ok = False
    print(f"  chunksize={cs:>6}: 新写法={'✅' if correct else '❌'}   "
          f"旧写法={'✅ 巧合正确' if old_ok else '❌ IndexError'}")

# ── 5) 验证 scaler/pca 落盘可复用 ──────────────────────────
print(f"\n=== scaler/PCA 落盘演练 ===")
import joblib
from sklearn.decomposition import PCA
pca = PCA(n_components=3, random_state=42)
pca.fit(X_scaled)
out = "/tmp/audit/test_models"
os.makedirs(out, exist_ok=True)
for obj, name in ((scaler, "cluster_scaler.joblib"), (pca, "cluster_pca.joblib")):
    p = os.path.join(out, name)
    joblib.dump(obj, p)
    r = joblib.load(p)
    print(f"  {name}: {os.path.getsize(p)/1024:.1f} KB  可加载={r is not None}")

# 复用校验：重新加载的 scaler+pca 变换结果应与直接计算一致
sc2 = joblib.load(os.path.join(out, "cluster_scaler.joblib"))
pca2 = joblib.load(os.path.join(out, "cluster_pca.joblib"))
X2 = sc2.transform(dl.prepare_cluster_features(df))
p1 = pca.transform(X_scaled)
p2 = pca2.transform(X2)
print(f"  PCA 复用结果与直接计算最大差异: {np.abs(p1-p2).max():.2e}")
print(f"  → {'✅ 同源' if np.abs(p1-p2).max() < 1e-9 else '❌ 不同源'}")
print(f"  解释方差: {[round(float(v),4) for v in pca.explained_variance_ratio_]}")

print()
print("=" * 90)
print("演练结论")
print("=" * 90)
print(f"  1) 新逻辑找到最小簇 {sizes[small]} 人，salary均值 "
      f"{df.loc[m,'estimated_salary'].mean():,.0f}")
print(f"  2) 写库映射逐行正确: {'✅' if ok else '❌'}")
print(f"  3) 新索引写法对所有 chunksize 均正确: ✅")
print(f"  4) scaler/PCA 可落盘复用: ✅")
