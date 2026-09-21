"""验证：「t-SNE 只用于展示」这句话到底有没有实际内容。

要回答的问题：
  Q1) 换 t-SNE 后，聚类结果本身有没有变？
  Q2) 图上看起来分得很开，真实可分性如何？（t-SNE 是否夸大分离）
  Q3) 我上条消息里「5-NN 44.2% → 98.8%」放在"实测改善"表里是否恰当？
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, pandas as pd, json
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
from sklearn.metrics import silhouette_score
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import cross_val_score

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
lab = df["cluster_id"].values.astype(int)

print("=" * 96)
print("Q1) 换 t-SNE 后，聚类结果本身变了吗？")
print("=" * 96)
meta = json.load(open("/app/saved_models/cluster_meta.json", encoding="utf-8"))
cur = {str(k): int(v) for k, v in sorted(pd.Series(lab).value_counts().items())}
meta_sizes = {str(k): int(v) for k, v in meta["cluster_sizes"].items()}
print(f"  meta.cluster_sizes = {meta_sizes}")
print(f"  meta.silhouette    = {meta['silhouette']}")
print(f"  DB 实际分布        = {cur}")
print(f"\n  → 两者一致: {cur == meta_sizes}")
print("     t-SNE 是**在聚类之后**跑的降维，对 cluster_id 没有任何影响。")

print()
print("=" * 96)
print("Q2) t-SNE 图看起来分得很开 —— 真实可分性如何？")
print("=" * 96)
rng = np.random.default_rng(42)
idx = rng.choice(len(Xs), 20000, replace=False)
sil_11d = silhouette_score(Xs[idx], lab[idx])
print(f"  11 维原始特征空间的 silhouette = {sil_11d:+.4f}   ← 聚类质量的诚实指标")

with np.load("/app/saved_models/cluster_tsne.npz") as z:
    emb = z["embedding"]; sidx = z["sample_index"]
sil_tsne = silhouette_score(emb, lab[sidx])
print(f"  t-SNE 2D 坐标上的 silhouette  = {sil_tsne:+.4f}   ← 视觉分离度")
print()
print(f"  比值 = {sil_tsne/sil_11d:.1f} 倍")
print("  t-SNE 坐标没有度量含义（它只保留邻域、主动拉开簇并挤压簇内），")
print("  所以这个倍数衡量的是「图看起来多分得开」，不是「聚类有多好」。")

print()
print("=" * 96)
print("Q2b) 诚实的可分性：两两簇在 11 维上能不能分开（不依赖任何降维）")
print("=" * 96)
cs = sorted(set(lab))
print(f"  {'簇对':>8} {'5-NN 二分类准确率':>18}   解读")
accs = []
for i in range(len(cs)):
    for j in range(i + 1, len(cs)):
        sub = np.where((lab == cs[i]) | (lab == cs[j]))[0]
        if len(sub) > 4000:
            sub = rng.choice(sub, 4000, replace=False)
        acc = cross_val_score(KNeighborsClassifier(5), Xs[sub], lab[sub], cv=3).mean()
        accs.append(acc)
        verdict = "几乎分不开" if acc < 0.7 else ("弱可分" if acc < 0.85 else "可分")
        print(f"  C{cs[i]}-C{cs[j]:<5} {acc*100:>17.1f}%   {verdict}")
print()
print(f"  两两可分性均值 = {np.mean(accs)*100:.1f}%")
print("  （50% = 完全随机；100% = 完美可分）")
print("  这是**不依赖任何降维**的诚实指标。")

print()
print("=" * 96)
print("Q3) 「5-NN 44.2% → 98.8%」这个对比是否恰当？")
print("=" * 96)
print("  在**同一批 15000 个客户**（t-SNE 的抽样集）上算 5-NN 标签恢复率：")
L = lab[sidx]
pca2 = PCA(n_components=2).fit_transform(Xs[sidx])
for tag, Z in [("11 维原始特征空间", Xs[sidx]),
               ("PCA 2D 坐标", pca2),
               ("t-SNE 2D 坐标", emb)]:
    acc = cross_val_score(KNeighborsClassifier(5), Z, L, cv=3).mean()
    print(f"    {tag:22s} {acc*100:>6.1f}%")
print()
print("  关键：t-SNE 的优化目标**就是**保留局部邻域 ——")
print("    在 11 维里互为近邻的点，在 t-SNE 图上也必然互为近邻。")
print("    所以「t-SNE 图上 5-NN 准确率高」是算法的**定义性质**，不是实证发现。")
print("    把它写进『实测改善』表，属于用同义反复冒充证据。")
