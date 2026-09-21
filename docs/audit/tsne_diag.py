"""诊断：t-SNE 文件已生成但接口仍走 PCA 回退。只读。

现象：
  /app/saved_models/cluster_tsne.npz 存在（134707 字节）
  meta.tsne.available = true
  但 /api/cluster/3d-scatter 的 method = None，data 与 PCA 路径一致

可能原因：
  A) _load_saved_tsne 返回 None（越界/形状/读取失败）
  B) 服务层的判断逻辑没走到 tsne 分支
  C) backend 进程用的是旧代码（未重启）
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, os
from app.database import SessionLocal
from app.services import clustering_service as cs

print("=" * 90)
print("一、直接测试 _load_saved_tsne")
print("=" * 90)
print(f"  CLUSTER_DIR = {cs.CLUSTER_DIR}")
p = cs.CLUSTER_DIR / "cluster_tsne.npz"
print(f"  文件存在: {p.exists()}  大小={p.stat().st_size if p.exists() else 0}")

if p.exists():
    with np.load(p) as z:
        emb = z["embedding"]; idx = z["sample_index"]
    print(f"  embedding.shape={emb.shape}  idx len={len(idx)} max={idx.max()}")

db = SessionLocal()
svc = cs.get_clustering_service(db)
try:
    r = svc._load_saved_tsne(100000)
    print(f"  _load_saved_tsne(100000) → {'None ❌' if r is None else 'OK ✅'}")
    if r is not None:
        print(f"    emb.shape={r[0].shape}")
except Exception as e:
    import traceback; traceback.print_exc()

print()
print("=" * 90)
print("二、调用 get_3d_scatter_data 看 method")
print("=" * 90)
out = svc.get_3d_scatter_data()
print(f"  method = {out.get('method')}")
print(f"  keys = {sorted(out.keys())}")
print(f"  plotted_points = {out.get('plotted_points')}")

print()
print("=" * 90)
print("三、检查服务层源码是否有 tsne 分支")
print("=" * 90)
import inspect
src = inspect.getsource(svc.get_3d_scatter_data)
print(f"  含 '_load_saved_tsne': {'_load_saved_tsne' in src}")
print(f"  含 'method=\"tsne\"': {'method=\"tsne\"' in src}")
print(f"  含 '_scatter_payload': {'_scatter_payload' in src}")
print(f"  源码行数: {len(src.splitlines())}")
# 打印关键行
for i, line in enumerate(src.splitlines(), 1):
    if 'tsne' in line.lower() or 'method' in line:
        print(f"    L{i}: {line.strip()[:100]}")
db.close()

print()
print("=" * 90)
print("四、backend 是否加载了新代码")
print("=" * 90)
import hashlib
for f in ["/app/app/services/clustering_service.py",
          "/app/app/celery_tasks/cluster.py"]:
    h = hashlib.md5(open(f, "rb").read()).hexdigest()
    print(f"  {os.path.basename(f):28s} md5={h}  行数={len(open(f,encoding='utf-8').read().splitlines())}")
