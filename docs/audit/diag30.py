"""第三十轮：客户列表服务的性能与非确定性缺陷。只读。

疑点：
  1) `get_customer_detail` 线性扫描 10 万条 list（O(n) per request）
  2) `_filtered_customers` 对每个请求都 `dict(c)` 复制 10 万条
  3) 排序 key：`c.get(key) if c.get(key) is not None else -1`
     —— 若某字段为 None，会被当成 -1；对 balance 排序时，
        **余额为 0 与 None 会混在一起**，且 asc 时 None 排最前
  4) risk_distribution 用 4 次 O(n) 扫描（可合并）
"""
import sys
sys.path.insert(0, "/tmp/audit")
import time
from app.database import SessionLocal
from app.services import risk_scoring as rs
from app.services import customer_service as cs

db = SessionLocal()

print("=" * 92)
print("一、get_scored_customers 的规模与结构")
print("=" * 92)
t = time.time()
sc = rs.get_scored_customers(db)
print(f"  条数 = {len(sc)}, 首次耗时 {time.time()-t:.2f}s（缓存后应很快）")
t = time.time(); sc = rs.get_scored_customers(db)
print(f"  缓存命中耗时 {time.time()-t:.4f}s")

print()
print("=" * 92)
print("二、get_customer_detail 的 O(n) 扫描代价")
print("=" * 92)
# 测最坏情况：查最后一个客户
last_cid = sc[-1]["customer_id"]
mid_cid = sc[len(sc)//2]["customer_id"]
first_cid = sc[0]["customer_id"]
for tag, cid in [("第一个", first_cid), ("中间", mid_cid), ("最后", last_cid)]:
    t = time.time()
    d = cs.get_customer_detail(db, cid)
    dt = (time.time() - t) * 1000
    print(f"  查{tag:4s}客户 {cid}: {dt:8.2f} ms  找到={d is not None}")
print(f"\n  ⚠ 每次详情请求都线性扫全表，且 `_active_order_ids` 还会再查一次库。")

print()
print("=" * 92)
print("三、排序的 None 处理缺陷")
print("=" * 92)
# 检查 scored 里各字段是否存在 None
fields = ["probability", "balance", "age", "credit_score", "expected_value"]
for f in fields:
    nones = sum(1 for c in sc if c.get(f) is None)
    print(f"  {f:16s} None 的条数 = {nones}")

print()
print("  实测：按 balance 升序排序，看 None 的位置")
bals = [c.get("balance") for c in sc]
print(f"    balance 取值: min={min(bals)}, max={max(bals)}")
print(f"    是否为 0 的条数: {sum(1 for b in bals if b == 0)}")

# 模拟 list_customers 的排序逻辑
items = [dict(c) for c in sc[:20000]]
key = "balance"
reverse = False   # asc
sorted_items = sorted(items, key=lambda c: c.get(key) if c.get(key) is not None else -1,
                      reverse=reverse)
print(f"\n    asc 排序后前 3 个 balance: {[x['balance'] for x in sorted_items[:3]]}")
print(f"    asc 排序后后 3 个 balance: {[x['balance'] for x in sorted_items[-3:]]}")

# 检查 NULL 值是否真的存在
null_bals = [c for c in sc if c.get("balance") is None]
print(f"\n    全量中 balance 为 None 的: {len(null_bals)}")

print()
print("=" * 92)
print("四、risk_distribution 的重复扫描")
print("=" * 92)
t = time.time()
for _ in range(5):
    rd = {lvl: sum(1 for c in sc if c["risk_level"] == lvl)
          for lvl in ("CRITICAL", "HIGH", "MEDIUM", "LOW")}
print(f"  4 次 O(n) 扫描 × 5 轮: {time.time()-t:.3f}s  (10 万条)")
print(f"  分布 = {rd}")
# 单次扫描对照
import collections
t = time.time()
for _ in range(5):
    rd2 = collections.Counter(c["risk_level"] for c in sc)
print(f"  单次 Counter × 5 轮:    {time.time()-t:.3f}s")
print(f"  差异: {(time.time()-t)/5*1000:.1f} ms/次" )
print(f"  结果一致: {dict(rd2) == rd}")

print()
print("=" * 92)
print("五、list_customers 端到端耗时（含每请求 dict 复制 10 万次）")
print("=" * 92)
t = time.time()
r = cs.list_customers(db, page=1, page_size=20)
print(f"  首次: {time.time()-t:.3f}s  total={r['total']}  pages={r['total_pages']}")
t = time.time()
r = cs.list_customers(db, page=1, page_size=20)
print(f"  再次: {time.time()-t:.3f}s")
t = time.time()
r = cs.list_customers(db, page=500, page_size=20, sort_by="balance", sort_order="asc")
print(f"  翻到第500页+按余额升序: {time.time()-t:.3f}s")

db.close()
