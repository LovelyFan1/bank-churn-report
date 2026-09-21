"""第三十四轮：核实子代理报告的关键发现 + 静态扫描的除零疑点。只读。

重点核实（每条都必须见实测证据，不采信推测）：
  A) /api/customers/{id} 是否真的缺少 has_credit_card / points_earned
  B) 工单时间戳是否按 UTC 原样返回（差 8 小时）
  C) data_loader.churn_rate_by / eda_service 的除零风险
"""
import sys
sys.path.insert(0, "/tmp/audit")
import json, urllib.request
from app.database import SessionLocal

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

print("=" * 92)
print("A) 客户详情接口返回的字段 vs 模型预测所需字段")
print("=" * 92)
d = get("/api/customers/C071081")
print(f"  详情返回键数 = {len(d)}")
keys = sorted(d.keys())
print(f"  全部键: {keys}")

# 模型预测需要的字段
needed = ["credit_score", "age", "tenure", "balance", "num_products",
          "has_credit_card", "is_active_member", "estimated_salary",
          "satisfaction_score", "points_earned", "geography", "gender"]
missing = [k for k in needed if k not in d]
print(f"\n  ⚠ 预测所需但**缺失**的字段: {missing}")
for k in missing:
    print(f"     - {k}")

print()
print("  实测影响：把详情返回的 dict 直接喂给 predict_single，看它用什么值")
from app.services.model_service import ModelService
svc = ModelService(db=None)
# 读 predict_single 的默认值逻辑
import inspect
src = inspect.getsource(svc.predict_single)
print("  predict_single 的取默认值部分:")
for line in src.splitlines():
    if "customer_data.get" in line:
        print(f"    {line.strip()}")

print()
print("=" * 92)
print("B) 工单时间戳口径")
print("=" * 92)
w = get("/api/work-orders?page=1&page_size=2")
items = w.get("items", [])
print(f"  total = {w.get('total')}")
for it in items[:2]:
    print(f"\n  工单 {it['id']}:")
    for k in ["created_at", "updated_at", "completed_at"]:
        print(f"    {k:14s} = {it.get(k)}")

# 与数据库真实值对比
db = SessionLocal()
from app.models.work_order import WorkOrder
o = db.query(WorkOrder).order_by(WorkOrder.id).first()
print(f"\n  DB 中 id={o.id} 的原始值:")
print(f"    created_at = {o.created_at}  (tzinfo={getattr(o.created_at,'tzinfo',None)})")
print(f"    updated_at = {o.updated_at}")
db.close()

print()
print("  现系统时间:")
import datetime
print(f"    UTC  now = {datetime.datetime.now(datetime.timezone.utc)}")
print(f"    本地 now = {datetime.datetime.now()}")
print(f"    ⚠ 若接口返回的 created_at 是 naive UTC，前端直接显示会差 8 小时")

print()
print("=" * 92)
print("C) 除零风险实测：churn_rate_by 与 eda_service")
print("=" * 92)
from app.services.data_loader import DataLoader
db = SessionLocal()
loader = DataLoader(db)
# churn_rate_by 对每个取值分组，若某组 total=0 会除零（实际不会，因 group by 只产生存在的组）
for col in ["geography", "gender", "num_products"]:
    try:
        r = loader.churn_rate_by(col)
        print(f"  churn_rate_by({col}): {len(r)} 组  ✅ 无除零")
    except Exception as e:
        print(f"  churn_rate_by({col}): ❌ {type(e).__name__}: {e}")

# eda_service 的 key-insights 等接口
from app.services.eda_service import EDAService
svc2 = EDAService(db)
for meth in ["get_churn_analysis", "get_age_distribution", "get_geography_distribution"]:
    if hasattr(svc2, meth):
        try:
            r = getattr(svc2, meth)()
            print(f"  {meth}: ✅ OK")
        except Exception as e:
            print(f"  {meth}: ❌ {type(e).__name__}: {e}")
db.close()

print()
print("=" * 92)
print("D) 前端 index.html 标题编码")
print("=" * 92)
import subprocess
r = subprocess.run(
    ["docker", "exec", "churn-frontend", "sh", "-lc", "cat /app/index.html"],
    capture_output=True, text=True, encoding="utf-8", errors="replace")
print(r.stdout[:500] if r.stdout else r.stderr[:300])
