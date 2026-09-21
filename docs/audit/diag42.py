"""第四十二轮：核实后端审计报告的关键发现（尤其是我自己引入的第 1 条）。

逐条实测，不采信报告结论：
  A) 【高】模型版本失效是否未覆盖 _engine_cache / _scored_cache   ← 我引入的
  B) 【高】客户表失效是否不传播到风险引擎
  C) 【高】/api/eda/distribution/{feature} 是否 500
  D) 【中】分级线两套口径是否真的不一致（212 人）
  E) 【中】net_profit 与 benefit/cost_fn 是否真的矛盾
  F) 【中】risk-info 冷启动是否返回写死的 0.7/0.3/0.1
"""
import sys
sys.path.insert(0, "/tmp/audit")
import json, urllib.request, time
from app.database import SessionLocal

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=240) as r:
        return json.loads(r.read().decode())

print("=" * 96)
print("A)【高】模型版本失效是否覆盖 _engine_cache / _scored_cache")
print("=" * 96)
from app.services import risk_scoring as rs
db = SessionLocal()
eng = rs._ensure_engine(db)
sc = rs.get_scored_customers(db)
print(f"  引擎已加载: raw is not None = {eng['raw'] is not None}")
print(f"  打分缓存已建: {sc is not None}")
print()
old_ver = rs._read_model_version()
print(f"  当前版本号 = {old_ver!r}")
print(f"  _engine_cache 有 version 字段吗: {'version' in eng}")
print(f"  _scored_cache 有 version 字段吗: {'version' in rs._scored_cache}")
print(f"  源码里 _ensure_engine 的守卫条件:")
import inspect
src = inspect.getsource(rs._ensure_engine)
for line in src.splitlines():
    if "CACHE_TTL" in line or "_engine_cache[" in line and "if" in line:
        print(f"    {line.strip()}")
print()
print("  → 若守卫不含 _read_model_version()，则重训后引擎不刷新（最长 10 分钟）")
guard_has_version = "_read_model_version" in src.split("def ")[1] if "def " in src else False
print(f"  _ensure_engine 守卫是否检查版本号: {guard_has_version}")

src2 = inspect.getsource(rs.get_scored_customers)
g2 = "_read_model_version" in src2
print(f"  get_scored_customers 守卫是否检查版本号: {g2}")

print()
print("  实测：bump 版本号后，引擎是否会重建")
import subprocess, os
# 记录当前 raw 的 id 与一个特征值
raw_before = eng["raw"]
import numpy as np
sum_before = float(raw_before.sum())
subprocess.run([sys.executable, "-c",
    "import sys; sys.path.insert(0,'/app'); from app.services import risk_scoring as rs; rs.bump_model_version(); print('bumped')"],
    capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "/app"}, cwd="/app")
print(f"  已 bump 版本号: {old_ver!r} -> {rs._read_model_version()!r}")
t0 = time.time()
eng2 = rs._ensure_engine(db)
dt = time.time() - t0
sum_after = float(eng2["raw"].sum())
print(f"  _ensure_engine 耗时 = {dt:.4f}s")
print(f"  raw 是否被重建（对象 id 变化）: {raw_before is not eng2['raw']}")
print(f"  raw.sum() 变化: {sum_before:.2f} -> {sum_after:.2f}")
if dt < 0.01 and raw_before is eng2["raw"]:
    print("  ❌ 确认：bump 版本号**不会**让 _engine_cache 失效 —— 审计报告第 1 条属实")
else:
    print("  ✅ 引擎已重建")

db.close()

print()
print("=" * 96)
print("C)【高】/api/eda/distribution/{feature} 是否 500")
print("=" * 96)
for f in ["age", "geography", "balance", "exited", "credit_score"]:
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:8000/api/eda/distribution/{f}", timeout=180)
        body = r.read().decode()
        print(f"  {f:14s} -> HTTP {r.status}  {len(body)} bytes")
    except urllib.error.HTTPError as e:
        print(f"  {f:14s} -> ❌ HTTP {e.code}")
    except Exception as e:
        print(f"  {f:14s} -> ❌ {type(e).__name__}: {str(e)[:50]}")

print()
print("=" * 96)
print("E)【中】net_profit 与 benefit/cost_fn 是否矛盾")
print("=" * 96)
from app.services.cost_benefit_service import CostBenefitService
db2 = SessionLocal()
an = CostBenefitService(db2).analyze_thresholds()
bad = 0
for row in an["thresholds"]:
    calc = row["benefit"] - row["cost_fn"] - row["cost_fp"]
    if abs(calc - row["net_profit"]) > 1:
        bad += 1
        if bad <= 3:
            print(f"  t={row['threshold']}: benefit={row['benefit']} "
                  f"cost_fn={row['cost_fn']} cost_fp={row['cost_fp']}")
            print(f"        benefit-cost_fn-cost_fp = {calc}  vs net_profit = {row['net_profit']}"
                  f"  差={calc-row['net_profit']:.0f}")
print(f"\n  19 档中不自洽: {bad}/{len(an['thresholds'])}")
db2.close()

print()
print("=" * 96)
print("F)【中】risk-info 冷启动是否返回写死的 0.7/0.3/0.1")
print("=" * 96)
import inspect as _i
src3 = _i.getsource(rs.get_risk_info)
print("  get_risk_info 源码中的冷路径分支:")
for line in src3.splitlines():
    if "0.7" in line or "0.3" in line or "0.1" in line:
        print(f"    {line.strip()}")
print()
print("  ⚠ 这是**冷路径**（_engine_cache['raw'] is None）的返回值")
print("     正常路径返回的是实测分位阈值")

print()
print("=" * 96)
print("D)【中】分级线两套口径")
print("=" * 96)
eng3 = rs._ensure_engine(db) if False else None
ri = get("/api/model/risk-info")
print(f"  /api/model/risk-info thresholds = {ri.get('thresholds')}")
mx = get("/api/portfolio/matrix")
print(f"  /api/portfolio/matrix thresholds = {mx.get('thresholds')}")
t1, t2 = ri.get("thresholds"), mx.get("thresholds")
if t1 and t2:
    same = all(abs(t1[k]-t2[k]) < 1e-9 for k in t1)
    print(f"  一致: {'✅' if same else '❌ 不一致'}")
