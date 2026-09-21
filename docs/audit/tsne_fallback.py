"""验证 t-SNE 不可用时是否安全回退到 PCA。只读。

模拟三种场景（在**临时目录**操作，不碰生产 saved_models）：
  1) npz 文件不存在（老产物 / 首次部署）
  2) npz 文件损坏
  3) sample_index 越界（客户表变更但嵌入未重算）
"""
import sys, os, shutil, tempfile, json
sys.path.insert(0, "/tmp/audit")
import numpy as np
from pathlib import Path
from app.database import SessionLocal
from app.services import clustering_service as cs

db = SessionLocal()
orig_dir = cs.CLUSTER_DIR
tmpdir = Path(tempfile.mkdtemp(prefix="tsne_fallback_"))

# 把真实产物复制到临时目录（只复制，不改原件）
for f in ["cluster_meta.json", "cluster_scaler.joblib", "cluster_pca.joblib"]:
    src = orig_dir / f
    if src.exists():
        shutil.copyfile(src, tmpdir / f)

try:
    cs.CLUSTER_DIR = tmpdir
    svc = cs.get_clustering_service(db)

    print("=" * 90)
    print("场景 1：npz 不存在（应回退 PCA）")
    print("=" * 90)
    out = svc.get_3d_scatter_data()
    print(f"  method = {out.get('method')}   {'✅ 回退 PCA' if out.get('method')=='pca' else '❌'}")
    print(f"  explained_variance 有值: {out.get('explained_variance') is not None}")
    print(f"  plotted_points = {out.get('plotted_points')}")
    print(f"  axis_range.x = {out.get('axis_range',{}).get('x')}")

    print()
    print("=" * 90)
    print("场景 2：npz 损坏（应回退 PCA）")
    print("=" * 90)
    (tmpdir / "cluster_tsne.npz").write_bytes(b"this is not a valid npz file")
    out = svc.get_3d_scatter_data()
    print(f"  method = {out.get('method')}   {'✅ 回退 PCA' if out.get('method')=='pca' else '❌'}")

    print()
    print("=" * 90)
    print("场景 3：sample_index 越界（应回退 PCA）")
    print("=" * 90)
    np.savez_compressed(tmpdir / "cluster_tsne.npz",
                        embedding=np.zeros((5, 2)),
                        sample_index=np.array([0, 1, 2, 3, 999999]))
    out = svc.get_3d_scatter_data()
    print(f"  method = {out.get('method')}   {'✅ 回退 PCA' if out.get('method')=='pca' else '❌'}")

    print()
    print("=" * 90)
    print("场景 4：正常的 npz（应使用 t-SNE）")
    print("=" * 90)
    emb = np.random.default_rng(0).normal(0, 10, (300, 2))
    idx = np.sort(np.random.default_rng(1).choice(100000, 300, replace=False))
    np.savez_compressed(tmpdir / "cluster_tsne.npz", embedding=emb, sample_index=idx)
    out = svc.get_3d_scatter_data()
    print(f"  method = {out.get('method')}   {'✅ 使用 t-SNE' if out.get('method')=='tsne' else '❌'}")
    print(f"  plotted_points = {out.get('plotted_points')}  (应为 300)")
    print(f"  z 是否恒为 0: {all(p[2] == 0 for g in out['data'] for p in g['points'])}")
    # 检查坐标是否来自我们的随机 embedding
    allx = [p[0] for g in out['data'] for p in g['points']]
    print(f"  x 范围 = [{min(allx):.2f}, {max(allx):.2f}]  "
          f"(随机数据的范围约 [{-3*10:.0f}, {3*10:.0f}])")

finally:
    cs.CLUSTER_DIR = orig_dir
    shutil.rmtree(tmpdir, ignore_errors=True)
    db.close()

print()
print("=" * 90)
print("生产目录未被修改")
print("=" * 90)
print(f"  cluster_tsne.npz 存在: {(orig_dir/'cluster_tsne.npz').exists()}")
print(f"  大小: {(orig_dir/'cluster_tsne.npz').stat().st_size} 字节")
