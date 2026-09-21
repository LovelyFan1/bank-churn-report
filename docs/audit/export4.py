"""重新导出对齐的可视化数据（含 num_products / cluster / salary）。只读。"""
import sys, json
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

from app.services.data_loader import prepare_cluster_features
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
pca = PCA(n_components=3).fit(Xs)
P = pca.transform(Xs)

N = 15000
rng = np.random.default_rng(42)
idx = rng.choice(len(Xs), N, replace=False)
ts = TSNE(n_components=2, random_state=42, perplexity=30, init="pca",
          max_iter=500, n_iter_without_progress=100).fit_transform(Xs[idx])

sub = rng.choice(N, 6000, replace=False)
out = {
  "pca2": P[idx][sub][:, :2].round(3).tolist(),
  "tsne": ts[sub].round(3).tolist(),
  "lab":  df["cluster_id"].values[idx][sub].tolist(),
  "np":   df["num_products"].values[idx][sub].tolist(),
  "sal":  df["estimated_salary"].values[idx][sub].round(0).tolist(),
  "bal0": (df["balance"].values[idx][sub] == 0).astype(int).tolist(),
}
json.dump(out, open("/tmp/audit/viz4.json", "w"))
print("已导出 viz4.json，含字段:", list(out.keys()))
print("  pca2 点数:", len(out["pca2"]), " lab 唯一值:", sorted(set(out["lab"])))
print("  np 唯一值:", sorted(set(out["np"])))
