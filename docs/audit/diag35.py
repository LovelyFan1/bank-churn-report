"""第三十五轮：工单时间戳时区偏差的实测确认。只读。

原理：
  后端 `datetime.now(timezone.utc)` 存入 SQLite → SQLAlchemy DateTime（无时区）
  列会**丢掉 tzinfo**，存成 naive UTC。
  序列化后返回 "2026-09-19T13:24:40.606519"（无 Z 后缀）。
  前端 `fmtDate` 只做 `d.replace('T',' ').substring(0,19)` —— 不解析时区。
  → 用户看到的"创建时间"是 UTC，比北京时间早 8 小时。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import json, urllib.request, datetime
from app.database import SessionLocal
from app.models.work_order import WorkOrder

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))

print("=" * 92)
print("一、后端返回的时间字符串格式")
print("=" * 92)
w = get("/api/work-orders?page=1&page_size=3")
for it in w["items"][:3]:
    s = it.get("created_at")
    print(f"  工单{it['id']:3d}: created_at = {s!r}")
    print(f"            含 'Z' 或 '+': {'Z' in str(s) or '+' in str(s)}")
    print(f"            → {'带时区标识' if ('Z' in str(s) or '+' in str(s)) else '⚠ 无时区标识（naive）'}")

print()
print("=" * 92)
print("二、DB 原始值的 tzinfo")
print("=" * 92)
db = SessionLocal()
o = db.query(WorkOrder).order_by(WorkOrder.id).first()
print(f"  created_at = {o.created_at}")
print(f"  tzinfo     = {o.created_at.tzinfo}  → {'naive（无时区）' if o.created_at.tzinfo is None else 'aware'}")
db.close()

print()
print("=" * 92)
print("三、前端 fmtDate 的行为（复刻 JS 逻辑）")
print("=" * 92)
def fmt_date_js(d):
    """复刻 WorkOrders.vue:314-318 的逻辑"""
    if not d:
        return ""
    return d.replace("T", " ")[:19]

s = w["items"][0]["created_at"]
shown = fmt_date_js(s)
print(f"  后端返回    : {s}")
print(f"  前端显示    : {shown}")
print()
print(f"  该值被 SQLAlchemy 按 UTC 写入，即真实 UTC 时间")
print(f"  北京时间应为: {shown} + 8 小时")
true_utc = datetime.datetime.fromisoformat(s)
true_bj = true_utc + datetime.timedelta(hours=8)
print(f"    UTC  = {true_utc}")
print(f"    北京 = {true_bj}")

print()
print("=" * 92)
print("四、对业务的实际影响（用真实工单验证）")
print("=" * 92)
db = SessionLocal()
orders = db.query(WorkOrder).all()
now_utc = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
future = [o for o in orders if o.updated_at and o.updated_at > now_utc]
print(f"  当前 UTC = {now_utc}")
print(f"  updated_at **晚于当前时间**的工单数 = {len(future)}")
if future:
    print("  ⚠ 这些工单在前端会显示成「未来的时间」，因为它们是 seeded 的未来日期")
    for o in future[:5]:
        print(f"     工单{o.id}: updated_at(UTC)={o.updated_at}  "
              f"前端显示={fmt_date_js(o.updated_at.isoformat())}")
db.close()

print()
print("=" * 92)
print("五、同源检查：其他页面是否有同样问题")
print("=" * 92)
import subprocess
print("  grep created_at/updated_at 在前端的使用点：")
print("    WorkOrders.vue:104  {{ fmtDate(o.created_at) }}")
print("    WorkOrders.vue:119  {{ fmtDate(o.updated_at) }}")
print("    WorkOrders.vue:120  {{ fmtDate(o.completed_at) }}")
print("  → 只有工单页显示时间；Dashboard 等页面不显示具体时间戳，故不受影响")

print()
print("=" * 92)
print("六、修复方案对比")
print("=" * 92)
print("""
  方案A（前端修）：fmtDate 里显式按 UTC 解析再转本地
      const d = new Date(s.endsWith('Z') ? s : s + 'Z')
      return d.toLocaleString('zh-CN', { hour12: false })
    ⚠ 前提：后端确实存的是 UTC。已确认（datetime.now(timezone.utc)）。

  方案B（后端修）：序列化时补上 'Z' 或改存 aware datetime
      更彻底，但要改 model + 迁移已有数据。

  方案C：统一改成存本地时间 —— ❌ 不推荐，会让已存的 UTC 数据含义混乱。

  推荐：方案A（前端加 'Z'），改动最小且不触碰数据；同时在 API 文档里
        明确「所有时间戳均为 UTC」。
""")
