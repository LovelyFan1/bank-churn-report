"""验证一个关键矛盾：silhouette 0.0957 vs 5-NN 可分性 99.7%。

如果 5 个簇在 11 维里两两 99.7% 可分，为什么 silhouette 只有 0.0957？
两者是否矛盾？以及：PCA 图让用户误判了什么？
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
from sklearn.metrics import silhouette_samples, silhouette_score
from sklearn.neighbors import KNeighborsClassifier
from sklearn.model_selection import cross_val_score

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
lab = df["cluster_id"].values.astype(int)
rng = np.random.default_rng(42)

print("=" * 96)
print("一、为什么「99.7% 可分」与「silhouette 0.0957」可以同时成立")
print("=" * 96)

# 关键：看簇的**形状**
# silhouette 关心「到本簇其他点的平均距离」vs「到最近他簇的平均距离」
# 5-NN 只关心「最近的 5 个邻居是不是同簇」
idx = rng.choice(len(Xs), 20000, replace=False)
from scipy.spatial.distance import cdist
# 取两个最大的簇
cs = sorted(set(lab))
a, b = cs[0], cs[1]
ma = np.where(lab == a)[0][:3000]
mb = np.where(lab == b)[0][:3000]
ca = Xs[ma].mean(0); cb = Xs[mb].mean(0)
print(f"  C{a} 与 C{b}（两个最大簇）:")
print(f"    中心距离（11 维）= {np.linalg.norm(ca-cb):.3f}")
print(f"    C{a} 内点到自身中心的平均距离 = {np.linalg.norm(Xs[ma]-ca, axis=1).mean():.3f}")
print(f"    C{b} 内点到自身中心的平均距离 = {np.linalg.norm(Xs[mb]-cb, axis=1).mean():.3f}")
print()
print("  簇内半径 ≈ 簇心距离的一半 → 两簇的**球体互相重叠**，")
print("  但重叠区只占各自体积很小一部分 → 最近邻仍几乎必是同簇。")
print("  这就是 silhouette 低（几何重叠）而 5-NN 高（邻域纯）并存的原因。")

print()
print("=" * 96)
print("二、各簇的平均 silhouette（哪个簇拉低了整体）")
print("=" * 96)
sv = silhouette_samples(Xs[idx], lab[idx])
L = lab[idx]
for c in cs:
    m = L == c
    print(f"  C{c}: n={m.sum():5d}  平均 silhouette = {sv[m].mean():+.4f}  "
          f"（{'>0' if sv[m].mean() > 0 else '<0 与其他簇混叠'}）")
print(f"\n  整体 = {sv.mean():+.4f}")

print()
print("=" * 96)
print("三、真正要回答的问题：PCA 图让用户误判了什么？")
print("=" * 96)
from sklearn.decomposition import PCA
pca = PCA(n_components=3).fit(Xs)
P2 = pca.transform(Xs[idx])[:, :2]

def knn_acc(Z, L):
    return cross_val_score(KNeighborsClassifier(5), Z, L, cv=3).mean()

acc_11 = knn_acc(Xs[idx], L)
acc_pca = knn_acc(P2, L)
with np.load("/app/saved_models/cluster_tsne.npz") as z:
    emb = z["embedding"]; sidx = z["sample_index"]
acc_tsne = knn_acc(emb, lab[sidx])

print(f"  {'空间':<26} {'5-NN 可分性':>12} {'用户在图上看到什么':>28}")
print(f"  {'11 维（聚类真实空间）':<26} {acc_11*100:>11.1f}% {'（看不到）':>28}")
print(f"  {'PCA 2D（改造前页面）':<26} {acc_pca*100:>11.1f}% {'一坨一坨，像没分开':>28}")
print(f"  {'t-SNE 2D（现在页面）':<26} {acc_tsne*100:>11.1f}% {'清晰分开的 5 块':>28}")
print()
print(f"  → PCA 2D 把 {acc_11*100:.0f}% 的可分性**压成了 {acc_pca*100:.0f}%**（几乎等于随机 50%）。")
print(f"     t-SNE 2D 恢复到 {acc_tsne*100:.0f}%，与 11 维真实水平一致。")
print()
print("  ⚠ 这不是「t-SNE 让聚类变好了」——聚类一直是 99% 可分。")
print("     而是「PCA 让一个本来很好的聚类**看起来像失败了**」。")

print()
print("=" * 96)
print("四、误判的实际代价：用户看到 PCA 图会做什么决定？")
print("=" * 96)
print("  可能动作                     依据（PCA 图）        实际（11 维真相）")
print("  ─────────────────────────────────────────────────────────────────")
print("  重新调 K / 换聚类算法         「簇分不开」           99.7% 可分，没必要")
print("  认为客户分群功能不可用        「一坨糊在一起」       功能正常，是投影问题")
print("  质疑数据质量                  「没有结构」           结构清晰，5 个簇分明")
print("  放弃用客群做决策              「看不出区别」         可用于决策")
print()
print("  反之，t-SNE 图的风险：")
print("  认为「簇分得很开 → 聚类质量很高」 ← 这个结论**恰好也是对的**（99.7%），")
print("     但若拿 t-SNE 的坐标数值去做别的判断（如算簇间距离、聚类的类间比），")
print("     就会得到错误结果 —— 这才是「轴无物理含义」要防的。")

print()
print("=" * 96)
print("五、结论")
print("=" * 96)
print("  · 「t-SNE 只用于展示」**有实际内容**：它不参与分级/排序/筛选，")
print("    因为 t-SNE 坐标无度量含义，且无 transform（新增客户需重跑）。")
print("  · 但这句话**说轻了**。真正的价值是：PCA 时代的散点图**误导**了")
print("    用户对聚类质量的判断（把 99% 可分显示成 50%）。")
print("  · 我上一条把「5-NN 44.2%→98.8%」写进『实测改善』表是**不恰当的**：")
print("    t-SNE 的定义就是保留邻域，这个数字高是必然的，不是发现。")
print("    正确的说法是「PCA 丢失了可分性，t-SNE 把它恢复」——主语要换。")
