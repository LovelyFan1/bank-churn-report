"""验证 t-SNE 端到端链路（在临时目录，不碰生产 saved_models）。只读数据。

验证点：
  1) _compute_tsne 的输出形状、抽样下标正确性
  2) 落盘 npz + 读回 是否一致
  3) 服务层 _load_saved_tsne 的越界保护是否生效
  4) embedding 与 cluster_id/exited 的对齐是否正确（关键！）
"""
import sys, os, json, shutil, tempfile
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
import app.celery_tasks.cluster as ct

X = prepare_cluster_features(df)
sc = StandardScaler(); Xs = sc.fit_transform(X)
lab = df["cluster_id"].values.astype(int)

print("=" * 94)
print("一、_compute_tsne 输出")
print("=" * 94)
emb, sidx = ct._compute_tsne(Xs, lab)
print(f"  embedding.shape = {emb.shape}")
print(f"  sample_index: len={len(sidx)} min={sidx.min()} max={sidx.max()} "
      f"唯一={len(np.unique(sidx))}")
print(f"  是否升序: {bool(np.all(np.diff(sidx) > 0))}")
print(f"  → 上限 {ct.TSNE_N_POINTS}，实际 {len(sidx)}")

print()
print("=" * 94)
print("二、分层抽样比例是否正确（各簇占比）")
print("=" * 94)
full = pd.Series(lab).value_counts(normalize=True).sort_index()
sub = pd.Series(lab[sidx]).value_counts(normalize=True).sort_index()
print(f"  {'簇':>4} {'全量占比':>10} {'抽样占比':>10} {'偏差':>9}")
for c in sorted(full.index):
    print(f"  {c:>4} {full[c]*100:>9.2f}% {sub.get(c,0)*100:>9.2f}% "
          f"{(sub.get(c,0)-full[c])*100:>+8.2f}pp")

print()
print("=" * 94)
print("三、落盘 / 读回（在临时目录）")
print("=" * 94)
tmpdir = tempfile.mkdtemp(prefix="tsne_test_")
orig = ct.CLUSTER_DIR
ct.CLUSTER_DIR = __import__("pathlib").Path(tmpdir)
try:
    final = ct.CLUSTER_DIR / "cluster_tsne.npz"
    # 与生产代码一致：临时名必须以 .npz 结尾
    tmp = final.with_name(final.stem + ".tmp.npz")
    np.savez_compressed(tmp, embedding=emb, sample_index=sidx)
    os.replace(tmp, final)
    print(f"  已写 {final.name}  大小={os.path.getsize(final)/1024:.1f} KB")
    with np.load(final) as z:
        e2, i2 = z["embedding"], z["sample_index"]
    print(f"  读回一致: emb={np.array_equal(emb, e2)}  idx={np.array_equal(sidx, i2)}")
finally:
    ct.CLUSTER_DIR = orig
    shutil.rmtree(tmpdir, ignore_errors=True)

print()
print("=" * 94)
print("四、对齐正确性验证（最关键）")
print("=" * 94)
# 模拟服务层：clustered.iloc[sample_index] 后，坐标是否对应正确的行
clustered = df[df["cluster_id"].notna()].copy()
plotted = clustered.iloc[sidx].copy()
plotted_emb = emb
print(f"  plotted 行数 = {len(plotted)}  emb 行数 = {len(plotted_emb)}")
# 关键：plotted 的 cluster_id 应与 lab[sidx] 完全一致
ok_lab = np.array_equal(plotted["cluster_id"].values.astype(int), lab[sidx])
print(f"  plotted.cluster_id == lab[sample_index]: {'✅' if ok_lab else '❌'}")
# 随机抽查 5 行：embedding 对应的是哪一行
rng = np.random.default_rng(0)
for k in rng.choice(len(sidx), 5, replace=False):
    orig_row = sidx[k]
    match_id = plotted.iloc[k]["id"] == df.iloc[orig_row]["id"]
    match_cl = plotted.iloc[k]["cluster_id"] == lab[orig_row]
    print(f"    抽样第{k:5d}项 → 原始行{orig_row:6d}  id一致={match_id} 簇一致={match_cl}")
print(f"  → {'✅ 对齐正确' if ok_lab else '❌ 对齐错误，会导致图上簇错位'}")

print()
print("=" * 94)
print("五、越界保护验证")
print("=" * 94)
tmpdir = tempfile.mkdtemp(prefix="tsne_guard_")
try:
    p = os.path.join(tmpdir, "cluster_tsne.npz")
    # 构造一个 sample_index 越界的文件
    np.savez_compressed(p, embedding=np.zeros((3,2)), sample_index=np.array([0,1,999999]))
    from app.services import clustering_service as cs
    svc = cs.ClusteringService.__new__(cs.ClusteringService)
    orig_dir = cs.CLUSTER_DIR
    cs.CLUSTER_DIR = __import__("pathlib").Path(tmpdir)
    r = svc._load_saved_tsne(n_rows=100000)
    cs.CLUSTER_DIR = orig_dir
    print(f"  越界文件 → _load_saved_tsne 返回: {'None（✅ 已拦截，会回退 PCA）' if r is None else '❌ 未拦截！'}")
    # 形状错误的文件
    np.savez_compressed(p, embedding=np.zeros((3,5)), sample_index=np.array([0,1,2]))
    cs.CLUSTER_DIR = __import__("pathlib").Path(tmpdir)
    r2 = svc._load_saved_tsne(n_rows=100000)
    cs.CLUSTER_DIR = orig_dir
    print(f"  形状错误文件 → 返回: {'None（✅ 已拦截）' if r2 is None else '❌ 未拦截！'}")
    # 不存在的文件
    os.remove(p)
    cs.CLUSTER_DIR = __import__("pathlib").Path(tmpdir)
    r3 = svc._load_saved_tsne(n_rows=100000)
    cs.CLUSTER_DIR = orig_dir
    print(f"  文件不存在 → 返回: {'None（✅）' if r3 is None else '❌'}")
finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
