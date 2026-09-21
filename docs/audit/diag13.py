"""第十三轮：验证新的相对命名实现（直接调用生产代码，非复刻）。只读。"""
import numpy as np
from app.database import SessionLocal
from app.services.clustering_service import ClusteringService, get_clustering_service

db = SessionLocal()
svc = get_clustering_service(db)

profiles = svc.get_cluster_profiles()
names = svc.get_cluster_names(profiles)

print("=" * 92)
print("一、新实现的输出（真实 10 万行数据）")
print("=" * 92)
print(f"\n  {'簇':>4} {'名称':<22} {'人数':>7} {'流失率':>7} {'余额':>10} "
      f"{'产品':>5} {'活跃':>5} {'薪资':>9} {'信用':>6}")
for c in profiles["clusters"]:
    cid = c["cluster_id"]; f = c["features"]
    print(f"  C{cid:<3} {names[cid]:<22} {c['count']:>7,} {c['churn_rate']:>6.1f}% "
          f"{f['balance']['mean']:>10,.0f} {f['num_products']['mean']:>5.2f} "
          f"{f['is_active_member']['mean']:>5.2f} {f['estimated_salary']['mean']:>9,.0f} "
          f"{f['credit_score']['mean']:>6.1f}")

uniq = set(names.values())
print(f"\n  不同簇名数 = {len(uniq)} / {len(names)}")
print(f"  名称列表 = {list(names.values())}")
if len(uniq) < len(names):
    dup = [n for n in uniq if list(names.values()).count(n) > 1]
    print(f"  ⚠ 重名: {dup}")

print()
print("=" * 92)
print("二、边界情况测试")
print("=" * 92)

def mk(id_, churn, bal, act, sal, prod=1.5, credit=650, tenure=5.0):
    return {"cluster_id": id_, "count": 1000, "churn_rate": churn,
            "features": {"balance": {"mean": bal}, "is_active_member": {"mean": act},
                         "estimated_salary": {"mean": sal}, "num_products": {"mean": prod},
                         "credit_score": {"mean": credit}, "tenure": {"mean": tenure}}}

cases = {
    "2 个簇": [mk(0, 30, 50000, 0.9, 100000), mk(1, 15, 90000, 0.3, 60000)],
    "3 个簇": [mk(0, 27.4, 45000, 0.9, 115000), mk(1, 19.9, 119000, 0.6, 62000),
              mk(2, 15.4, 65000, 0.04, 114000)],
    "5 个簇(真实)": [
        mk(0, 15.17, 45408, 0.90, 115177, 1.80, 669.45, 5.21),
        mk(1, 22.22, 119386, 0.59, 62651, 1.20, 656.12, 6.09),
        mk(2, 25.06, 65272, 0.04, 114062, 1.61, 625.12, 3.80),
        mk(3, 20.33, 117878, 0.71, 136633, 1.19, 623.95, 5.16),
        mk(4, 19.69, 44533, 0.32, 75593, 1.77, 673.99, 4.23)],
    "完全相同的 3 个簇": [mk(0, 20, 50000, 0.5, 80000), mk(1, 20, 50000, 0.5, 80000),
                        mk(2, 20, 50000, 0.5, 80000)],
    "单簇": [mk(0, 20, 50000, 0.5, 80000)],
}
for label, cls in cases.items():
    got = svc.get_cluster_names({"clusters": cls})
    uniq_n = len(set(got.values()))
    flag = "✅" if uniq_n == len(cls) else ("—" if label.startswith("完全相同") else "⚠")
    print(f"  {flag} {label:<18} → {uniq_n}/{len(cls)} 个不同名  {list(got.values())}")

print()
print("=" * 92)
print("三、与旧实现对照")
print("=" * 92)
old_names = {0: "中等价值客户", 1: "中等价值客户", 2: "低活跃沉默客户",
             3: "中等价值客户", 4: "中等价值客户"}
print(f"  {'簇':>4} {'旧名称':<18} {'新名称':<22}")
for c in profiles["clusters"]:
    cid = c["cluster_id"]
    print(f"  C{cid:<3} {old_names[cid]:<18} {names[cid]:<22}")
print(f"\n  旧: {len(set(old_names.values()))}/5 个不同名")
print(f"  新: {len(set(names.values()))}/5 个不同名")

# 幂等性：同样输入多次调用结果一致
r1 = svc.get_cluster_names(profiles)
r2 = svc.get_cluster_names(profiles)
print(f"\n  幂等性: {'✅ 一致' if r1 == r2 else '❌ 不一致'}")

db.close()
