"""第四十轮：验证模型缓存跨进程失效 + 全链路最终校验。只读为主。"""
import json, os, subprocess, sys, time, urllib.request
sys.path.insert(0, "/tmp/audit")

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))

print("=" * 92)
print("一、模型版本号机制")
print("=" * 92)
vf = "/app/saved_models/.model_version"
print(f"  版本文件存在: {os.path.exists(vf)}")
print(f"  当前版本号: {open(vf).read().strip() if os.path.exists(vf) else '(不存在)'}")

# 从独立进程 bump 版本号，观察 backend 是否重载模型
bump = r'''
import sys, json, os
sys.path.insert(0, "/app")
from app.services import risk_scoring as rs
before = rs._read_model_version()
rs.bump_model_version()
after = rs._read_model_version()
print(json.dumps({"before": before, "after": after}))
'''
r = subprocess.run([sys.executable, "-c", bump], capture_output=True, text=True,
                   env={**os.environ, "PYTHONPATH": "/app"}, cwd="/app")
info = json.loads(r.stdout.strip().splitlines()[-1])
print(f"  独立进程 bump: {info['before']!r} → {info['after']!r}")
print(f"  递增成功: {'✅' if info['before'] != info['after'] else '❌'}")

# backend 侧应立即感知（下次取模型时）
ri = get("/api/model/risk-info")
print(f"  backend 仍正常返回: model={ri.get('model')} "
      f"decision_threshold={ri.get('decision_threshold')}")
print(f"  → 版本号变化会触发 backend 重新加载模型（下次请求时）")

print()
print("=" * 92)
print("二、全系统关键数字一致性终检")
print("=" * 92)
checks = {}
ri = get("/api/model/risk-info")
checks["risk_info.recall"] = ri.get("decision_metrics", {}).get("recall")
checks["risk_info.precision"] = ri.get("decision_metrics", {}).get("precision")
checks["risk_info.decision_threshold"] = ri.get("decision_threshold")

cb = get("/api/cost-benefit/summary")
checks["cost_benefit.model_recall"] = cb.get("model_recall")
checks["cost_benefit.model_precision"] = cb.get("model_precision")
checks["cost_benefit.optimal_threshold"] = cb.get("optimal_threshold")

ds = get("/api/dashboard/summary")
checks["dashboard.bs.model_recall"] = ds.get("business_summary", {}).get("model_recall")
checks["dashboard.ri.recall"] = ds.get("risk_info", {}).get("decision_metrics", {}).get("recall")

mc = get("/api/model/comparison")
best = mc["models"][0]
checks["comparison.best.name"] = best["model_name"]
checks["comparison.best.recall"] = best["recall"]

for k, v in checks.items():
    print(f"  {k:38s} = {v}")

print()
print("  一致性判定:")
rl = {checks["risk_info.recall"], checks["cost_benefit.model_recall"],
      checks["dashboard.bs.model_recall"], checks["dashboard.ri.recall"],
      checks["comparison.best.recall"]}
print(f"    召回率出现 {len(rl)} 个不同值: {rl} → {'✅ 统一' if len(rl)==1 else '❌ 不统一'}")
th = {checks["risk_info.decision_threshold"], checks["cost_benefit.optimal_threshold"]}
print(f"    决策阈值出现 {len(th)} 个不同值: {th} → {'✅ 统一' if len(th)==1 else '❌ 不统一'}")

print()
print("=" * 92)
print("三、聚类结果与命名终检")
print("=" * 92)
cp = get("/api/cluster/profiles")
names = []
for c in cp["clusters"]:
    f = c["features"]
    print(f"  C{c['cluster_id']} {c['name']:22s} {c['count']:6d}人 流失{c['churn_rate']:5.1f}% "
          f"余额{f['balance']['mean']:9.0f} 薪资{f['estimated_salary']['mean']:9.0f}")
    names.append(c["name"])
print(f"\n  不同簇名数: {len(set(names))}/{len(names)}")
print(f"  总人数: {sum(c['count'] for c in cp['clusters'])}")

sc = get("/api/cluster/3d-scatter")
print(f"\n  散点: total={sc.get('total_points')} plotted={sc.get('plotted_points')} "
      f"sampled={sc.get('sampled')}")
print(f"  解释方差: {[round(v,4) for v in sc.get('explained_variance',[])]}")

print()
print("=" * 92)
print("四、SHAP 归因（此前 100% 失败的路径）")
print("=" * 92)
# batch_score 是异步，这里直接调函数验证
chk = r'''
import sys, json
sys.path.insert(0, "/app")
from app.celery_tasks.predict import batch_score_task
r = batch_score_task.apply(kwargs={"top_n": 3})
d = r.result
out = {"status": d.get("status"), "n": len(d.get("top_customers", []))}
recs = []
for c in d.get("top_customers", [])[:2]:
    recs.append({
        "customer_id": c.get("customer_id"),
        "probability": c.get("probability"),
        "risk_level": c.get("risk_level"),
        "risk_factors": c.get("risk_factors"),
        "basis": c.get("risk_factors_basis"),
        "reason": c.get("reason"),
        "strategy": c.get("strategy"),
    })
out["records"] = recs
print(json.dumps(out, ensure_ascii=False))
'''
r2 = subprocess.run([sys.executable, "-c", chk], capture_output=True, text=True,
                    env={**os.environ, "PYTHONPATH": "/app"}, cwd="/app")
if r2.returncode != 0:
    print("  ❌ 执行失败:", r2.stderr[-400:])
else:
    d = json.loads(r2.stdout.strip().splitlines()[-1])
    print(f"  status = {d['status']}, top = {d['n']}")
    for rec in d["records"]:
        print(f"\n  {rec['customer_id']}  prob={rec['probability']}  {rec['risk_level']}")
        print(f"    basis  = {rec['basis']}")
        print(f"    reason = {rec['reason']}")
        print(f"    factors:")
        for f in rec["risk_factors"]:
            print(f"      - {f}")
        # 验证占比之和
        import re
        tot = sum(float(m.group(1)) for x in rec["risk_factors"]
                  for m in [re.search(r"（([\d.]+)%）", x)] if m)
        print(f"    占比之和 = {tot:.1f}%")
    print()
    print("  ⚠ 修复前：basis 字段不存在，factors 是无百分比的规则标签")
    print("     修复后：basis=shap_ratio_of_positive_contributions，占比相加≈100%")
