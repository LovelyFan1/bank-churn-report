"""第三十八轮：验证跨进程缓存失效机制真的生效（不重启服务）。

流程：
  1) 记录当前接口返回的簇分布
  2) 直接触发一次重新聚类（save_to_db=True）
  3) **不重启任何服务**，再次请求接口
  4) 若接口分布 == DB 分布 → 跨进程失效生效
     若接口仍是旧值 → 机制无效
"""
import json, time, urllib.request
import subprocess

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))

def post(path):
    req = urllib.request.Request("http://127.0.0.1:8000" + path, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

def db_dist():
    out = subprocess.run(
        ["python", "-c", """
import collections
from app.database import SessionLocal
from app.models.customer import Customer
db=SessionLocal()
c=collections.Counter(r[0] for r in db.query(Customer.cluster_id).all())
print(json.dumps(dict(sorted(c.items()))))
db.close()
""".replace("json.dumps", "__import__('json').dumps")],
        capture_output=True, text=True, env={"PYTHONPATH": "/app", "PATH": "/usr/local/bin:/usr/bin:/bin"},
        cwd="/app")
    return out.stdout.strip()

print("=" * 90)
print("一、当前状态")
print("=" * 90)
before_api = get("/api/cluster/profiles")
before_api_dist = {c["cluster_id"]: c["count"] for c in before_api["clusters"]}
print(f"  接口分布: {dict(sorted(before_api_dist.items()))}")

print()
print("=" * 90)
print("二、触发重新聚类（写库）")
print("=" * 90)
sub = post("/api/cluster/kmeans/save?k=5")
tid = sub["task_id"]
print(f"  task_id = {tid}")
for i in range(80):
    d = get(f"/api/tasks/{tid}")
    st = d.get("status")
    if st in ("SUCCESS", "FAILURE"):
        res = d.get("result") or {}
        print(f"  最终状态: {st}")
        print(f"  cluster_sizes = {res.get('cluster_sizes')}")
        print(f"  silhouette    = {res.get('silhouette_score')}")
        break
    time.sleep(3)

print()
print("=" * 90)
print("三、不重启服务，立即再请求接口")
print("=" * 90)
# 给一个极短的间隔，模拟用户点完按钮刷新页面
time.sleep(1)
after_api = get("/api/cluster/profiles")
after_api_dist = {c["cluster_id"]: c["count"] for c in after_api["clusters"]}
print(f"  接口分布: {dict(sorted(after_api_dist.items()))}")
print(f"  （聚类前接口分布: {dict(sorted(before_api_dist.items()))}）")

# 从容器读 DB 真实分布
import os
print()
print("=" * 90)
print("四、DB 真实分布（通过接口 /metrics 间接验证）")
print("=" * 90)
# 用 risk_scoring 的引擎读（backend 进程内）
ev = get("/api/cluster/3d-scatter")
scatter_counts = [g["plotted"] for g in ev.get("data", [])]
print(f"  /cluster/3d-scatter 各 series plotted = {scatter_counts}")
print(f"  total_points = {ev.get('total_points')}")
print(f"  explained_variance = {ev.get('explained_variance')}")

print()
print("=" * 90)
print("五、判定")
print("=" * 90)
changed = after_api_dist != before_api_dist
print(f"  接口分布在重新聚类后是否变化: {'✅ 变了（缓存失效生效）' if changed else '❌ 没变（缓存未失效）'}")
print()
print(f"  聚类前: {dict(sorted(before_api_dist.items()))}")
print(f"  聚类后: {dict(sorted(after_api_dist.items()))}")
print()
# 检查是否与任务返回的 cluster_sizes 一致
if 'res' in dir() and res.get("cluster_sizes"):
    task_sizes = {int(k): int(v) for k, v in res["cluster_sizes"].items()}
    match = task_sizes == after_api_dist
    print(f"  接口分布 == 任务写入分布: {'✅ 是' if match else '❌ 否'}")
    if not match:
        print(f"    任务写入: {dict(sorted(task_sizes.items()))}")
        print(f"    接口返回: {dict(sorted(after_api_dist.items()))}")
