"""第三十七轮：确认「重新聚类后接口仍返回旧数据」的缓存缺陷。

现象（已观察）：
    DB  cluster_id 分布 = {0:28382, 1:23748, 2:25539, 3:21080, 4:1251}   ← 新
    /api/cluster/profiles 返回 count = 25577, 22760, 22649, 14343, 14671  ← 旧

推断：clustering_service._get_dataframe() 走 get_cached_customer_df()，
      该缓存 TTL=3600 秒，且**聚类任务完成后没有任何机制通知 backend 失效**。
      Celery worker 与 backend 是不同进程，invalidate_customer_cache()
      只清自己的进程内存。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import json, urllib.request, time, os
from app.database import SessionLocal
from app.models.customer import Customer
import collections

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

print("=" * 92)
print("一、DB 真实分布 vs 接口返回分布")
print("=" * 92)
db = SessionLocal()
db_dist = dict(sorted(collections.Counter(
    r[0] for r in db.query(Customer.cluster_id).all()).items()))
db.close()
print(f"  DB                : {db_dist}")

prof = get("/api/cluster/profiles")
api_dist = {c["cluster_id"]: c["count"] for c in prof["clusters"]}
print(f"  /cluster/profiles : {dict(sorted(api_dist.items()))}")

same = db_dist == api_dist
print(f"\n  一致? {'✅ 是' if same else '❌ 否 —— 接口返回的是过期缓存数据'}")

print()
print("=" * 92)
print("二、确认缓存是根因")
print("=" * 92)
from app.services import data_loader as dl
from app.services import risk_scoring as rs

# 找到 backend 进程里缓存的 df（通过接口无法直接看，但可推断）
meta = get("/api/cluster/profiles")
print(f"  接口返回的簇人数合计 = {sum(api_dist.values())}")

# cluster_meta.json 的 mtime
mp = "/app/saved_models/cluster_meta.json"
print(f"\n  cluster_meta.json mtime = {time.strftime('%H:%M:%S', time.localtime(os.path.getmtime(mp)))}")
meta_json = json.load(open(mp, encoding="utf-8"))
print(f"  meta 里的 cluster_sizes = {meta_json['cluster_sizes']}")
print(f"  meta 里的 algorithm     = {meta_json.get('algorithm')}")
print(f"  meta 里的 silhouette    = {meta_json.get('silhouette')}")

print()
print("=" * 92)
print("三、接口返回的 silhouette 是哪个")
print("=" * 92)
# 分位数阈值接口
ri = get("/api/model/risk-info")
print(f"  /api/model/risk-info 的 thresholds = {ri.get('thresholds')}")

# 聚类页用的接口只返回 profiles 和 3d-scatter
sc = get("/api/cluster/3d-scatter")
print(f"  /api/cluster/3d-scatter 的 series 数 = {len(sc.get('data', []))}")
print(f"    各 series plotted = {[g['plotted'] for g in sc.get('data', [])]}")
print(f"    explained_variance = {sc.get('explained_variance')}")
print(f"    ⚠ 若这里也是旧值，说明散点图同样读到过期缓存")

print()
print("=" * 92)
print("四、缓存 TTL 与失效路径")
print("=" * 92)
print(f"  data_loader.CUSTOMER_CACHE_TTL = {dl.CUSTOMER_CACHE_TTL} 秒 "
      f"({dl.CUSTOMER_CACHE_TTL/60:.0f} 分钟)")
print("""
  失效途径只有两条：
    1) invalidate_customer_cache() —— 但它是**进程内**函数。
       Celery worker 调用它，只清 worker 自己的内存；backend 进程不知道。
    2) TTL 到期（1 小时）或 backend 重启。

  即：用户点「重新聚类」→ 任务成功 → 页面刷新 → **最长 1 小时看不到新结果**。
  这解释了为什么"重新聚类按钮像没反应"。
""")

print()
print("=" * 92)
print("五、验证：重启 backend 后是否恢复一致")
print("=" * 92)
print("  （这一步需要重启，稍后单独执行）")
print(f"  预期：重启后 /cluster/profiles 应返回 {dict(sorted(db_dist.items()))}")
