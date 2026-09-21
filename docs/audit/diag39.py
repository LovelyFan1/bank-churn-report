"""第三十九轮：用可区分的数据验证跨进程缓存失效机制。

⚠ 上一轮测试无效的原因：聚类是确定性的（同 random_state），
   两次结果逐位相同，"接口没变"无法区分「缓存生效」与「缓存未失效」。

本轮的判定方法：
  A) 从**独立进程**（模拟 Celery worker）写入一个**明显不同**的 cluster_id，
     并调用 invalidate_customer_cache()；
  B) 不重启 backend，请求接口；
  C) 若接口返回新值 → 机制生效；若仍是旧值 → 机制无效。
  D) 无论结果如何，最后**恢复原数据**。
"""
import json, os, subprocess, urllib.request, time, sys
sys.path.insert(0, "/tmp/audit")

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))

print("=" * 92)
print("一、基线：接口当前返回的分布")
print("=" * 92)
base = get("/api/cluster/profiles")
base_dist = {c["cluster_id"]: c["count"] for c in base["clusters"]}
print(f"  接口分布 = {dict(sorted(base_dist.items()))}")

# 从独立进程读 DB 真实分布 + 写入标记
MARKER = 9
script = r'''
import collections, json, sys
sys.path.insert(0, "/app")
from app.database import SessionLocal
from app.models.customer import Customer
from app.services.data_loader import invalidate_customer_cache, _read_cache_version

db = SessionLocal()
before = collections.Counter(r[0] for r in db.query(Customer.cluster_id).all())
# 把 C000001..C000010 改为 MARKER（明显不同于任何现有簇）
targets = [f"C{i:06d}" for i in range(1, 11)]
db.query(Customer).filter(Customer.customer_id.in_(targets)).update(
    {"cluster_id": MARKER}, synchronize_session=False)
db.commit()
after = collections.Counter(r[0] for r in db.query(Customer.cluster_id).all())
ver_before = _read_cache_version()
invalidate_customer_cache()      # ← 模拟 Celery worker 的失效调用
ver_after = _read_cache_version()
db.close()
print(json.dumps({
    "before": {str(k): v for k, v in sorted(before.items(), key=lambda x: (x[0] is None, x[0]))},
    "after":  {str(k): v for k, v in sorted(after.items(), key=lambda x: (x[0] is None, x[0]))},
    "version_before": ver_before,
    "version_after": ver_after,
}, ensure_ascii=False))
'''.replace("MARKER", str(MARKER))

print()
print("=" * 92)
print("二、从独立进程写入标记数据 + 递增版本号")
print("=" * 92)
r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                   env={**os.environ, "PYTHONPATH": "/app"}, cwd="/app")
if r.returncode != 0:
    print("  ❌ 失败:", r.stderr[-500:])
    raise SystemExit(1)
info = json.loads(r.stdout.strip().splitlines()[-1])
print(f"  写入前 DB 分布 = {info['before']}")
print(f"  写入后 DB 分布 = {info['after']}")
print(f"  版本号: {info['version_before']!r} → {info['version_after']!r}")
bumped = info['version_before'] != info['version_after']
print(f"  版本号是否递增: {'✅ 是' if bumped else '❌ 否'}")

print()
print("=" * 92)
print("三、不重启 backend，立即请求接口")
print("=" * 92)
time.sleep(1)
now = get("/api/cluster/profiles")
now_dist = {c["cluster_id"]: c["count"] for c in now["clusters"]}
print(f"  接口分布 = {dict(sorted(now_dist.items()))}")

expect = {int(k): v for k, v in info["after"].items()}
matched = now_dist == expect
print()
print("=" * 92)
print("四、判定")
print("=" * 92)
print(f"  期望分布 = {dict(sorted(expect.items()))}")
print(f"  接口返回 = {dict(sorted(now_dist.items()))}")
print(f"  一致: {'✅ 是 —— 跨进程缓存失效**生效**' if matched else '❌ 否 —— 机制未生效'}")
if MARKER in now_dist:
    print(f"  ✅ 接口已能看到新增的簇 {MARKER}（{now_dist[MARKER]} 人）")
else:
    print(f"  ❌ 接口看不到簇 {MARKER}，说明仍在用旧缓存")

print()
print("=" * 92)
print("五、恢复原数据")
print("=" * 92)
restore = r'''
import sys, json
sys.path.insert(0, "/app")
from app.database import SessionLocal
from app.models.customer import Customer
from app.services.data_loader import invalidate_customer_cache
db = SessionLocal()
# 重新计算真实聚类标签（确定性可复现）并写回
from app.services.data_loader import prepare_cluster_features, get_cached_customer_df
import numpy as np
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
df = get_cached_customer_df(db)
X = prepare_cluster_features(df)
Xs = StandardScaler().fit_transform(X)
km = KMeans(n_clusters=5, random_state=42, n_init=10)
lab = km.fit_predict(Xs)
mappings = [{"id": int(i), "cluster_id": int(l)} for i, l in zip(df["id"].values, lab)]
db.bulk_update_mappings(Customer, mappings)
db.commit()
import collections
c = collections.Counter(r[0] for r in db.query(Customer.cluster_id).all())
invalidate_customer_cache()
db.close()
print(json.dumps({str(k): v for k, v in sorted(c.items())}))
'''
r2 = subprocess.run([sys.executable, "-c", restore], capture_output=True, text=True,
                    env={**os.environ, "PYTHONPATH": "/app"}, cwd="/app")
if r2.returncode != 0:
    print("  ❌ 恢复失败:", r2.stderr[-400:])
else:
    print(f"  ✅ 已恢复: {r2.stdout.strip().splitlines()[-1]}")

time.sleep(1)
final = get("/api/cluster/profiles")
final_dist = {c["cluster_id"]: c["count"] for c in final["clusters"]}
print(f"  接口复查 = {dict(sorted(final_dist.items()))}")
print(f"  恢复成功: {'✅' if 9 not in final_dist else '❌ 仍残留标记簇'}")
