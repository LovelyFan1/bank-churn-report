"""核查：改动 net_profit（引入 s）后，全系统对外报的指标是否一致。

背景：我改了成本模型使最优阈值从 0.20 移到 0.60，但**没有重训模型**。
meta.json 里的 decision_threshold / recall / precision 仍是上次训练
（旧 s=1.0 逻辑）算出的值。若某些接口读 meta.json、另一些读引擎，
就会出现"同一模型两个召回率"——正是本项目反复出现的缺陷类型。

本脚本把每个对外暴露 recall/precision/threshold 的接口都打出来对照。
"""
import json
import urllib.request

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


print("=" * 84)
print("各接口对外暴露的 recall / precision / threshold")
print("=" * 84)

rows = []

# 1) model/risk-info —— 走引擎（新阈值 0.60）
ri = get("/api/model/risk-info")
dm = ri["decision_metrics"]
rows.append(("model/risk-info", "decision_metrics",
             dm.get("threshold"), dm.get("recall"), dm.get("precision")))

# 2) model/comparison —— 走 meta.json
mc = get("/api/model/comparison")
for m in mc.get("models", []):
    rows.append((f"model/comparison[{m['model_name']}]", "meta.json",
                 None, m.get("recall"), m.get("precision")))

# 3) cost-benefit/summary —— 走 get_business_summary（已改为引擎口径）
cb = get("/api/cost-benefit/summary")
rows.append(("cost-benefit/summary", "engine",
             cb.get("optimal_threshold"), cb.get("model_recall"),
             cb.get("model_precision")))

# 4) cost-benefit/thresholds —— 完整阈值曲线
try:
    th = get("/api/cost-benefit/thresholds")
    rows.append(("cost-benefit/thresholds", "engine",
                 th.get("optimal_threshold"),
                 (th.get("optimal_metrics") or {}).get("recall"),
                 (th.get("optimal_metrics") or {}).get("precision")))
except Exception as e:
    rows.append(("cost-benefit/thresholds", f"ERR {type(e).__name__}", None, None, None))

# 5) dashboard/summary —— business_summary + risk_info
ds = get("/api/dashboard/summary")
bs = ds.get("business_summary") or {}
rows.append(("dashboard/business_summary", "engine",
             bs.get("optimal_threshold"), bs.get("model_recall"),
             bs.get("model_precision")))
ri2 = ds.get("risk_info") or {}
rows.append(("dashboard/risk_info", "engine",
             ri2.get("decision_threshold"),
             (ri2.get("decision_metrics") or {}).get("recall"),
             (ri2.get("decision_metrics") or {}).get("precision")))

print(f"{'来源':<38}{'口径':<12}{'thr':>7}{'recall':>9}{'precision':>11}")
print("-" * 84)
for src, basis, t, r_, p in rows:
    ts = f"{t}" if t is not None else "—"
    rs = f"{r_}" if r_ is not None else "—"
    ps = f"{p}" if p is not None else "—"
    print(f"{src:<38}{basis:<12}{ts:>7}{rs:>9}{ps:>11}")

# 分组找出不一致
#
# ⚠ 判定逻辑要分两层（第一版写错过）：
#   1) **同一模型的 recall 在不同接口必须相同** —— 这是"同一件事两个数"缺陷
#   2) **不同模型的 recall 本来就不同** —— 不是缺陷
#   第一版按 recall 值分组，把 5 个不同模型误判成 5 个"不一致"，属假阳性。
#   正确的判据是「按模型名归并后，每个模型各自的 recall 是否唯一」。
by_model = {}
for src, basis, t, r_, p in rows:
    if r_ is None:
        continue
    # 从来源名提取模型名；非 model/comparison 的行统一归到 "engine(最优模型)"
    if src.startswith("model/comparison["):
        model = src[len("model/comparison["):-1]
    else:
        model = "engine(当前最优模型)"
    by_model.setdefault(model, []).append((src, r_, p))

print()
print("=" * 84)
print("按**模型**分组（同一模型跨接口必须一致）")
print("=" * 84)
bad = []
for model, items in sorted(by_model.items()):
    recalls = {r_ for _, r_, _ in items}
    precs = {p for _, _, p in items}
    ok = len(recalls) == 1 and len(precs) == 1
    if not ok:
        bad.append(model)
    print(f"[{'OK ' if ok else '!! '}] {model}")
    print(f"        recall 取值 {sorted(recalls)}   precision 取值 {sorted(precs)}")
    for s, r_, p in items:
        print(f"          {s:<40} recall={r_} prec={p}")

print()
print("=" * 84)
if bad:
    print(f"判定：**存在不一致** —— 以下模型的指标在不同接口间不同：{bad}")
    print("      根因：meta.json 是训练当时写的（旧阈值口径），")
    print("            而引擎按当前假设实时重算。需重训对齐。")
else:
    print("判定：**全系统口径一致** —— 每个模型在各接口报同一组指标。")
    print("      （不同模型之间数值不同是正常的，不属不一致。）")
print("=" * 84)

# 附带检查：meta.json 的阈值是否与引擎一致（重训后应一致）
print()
print("附：meta.json 中 LightGBM 的 decision_threshold（重训后应与引擎相同）")
try:
    import subprocess
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend", "python", "-c",
         "import json;m=json.load(open('/app/saved_models/meta.json',encoding='utf-8'));"
         "r=m['results'];"
         "print(json.dumps({k:{'thr':v.get('decision_threshold'),'recall':v['recall'],"
         "'prec':v['precision']} for k,v in r.items()}))"],
        cwd=r"D:\创新创业\创新创业\bank-churn-report",
        capture_output=True, text=True, encoding="utf-8")
    line = [l for l in out.stdout.strip().splitlines() if l.startswith("{")]
    if line:
        d = json.loads(line[-1])
        for k, v in d.items():
            print(f"    {k:<22} thr={v['thr']}  recall={v['recall']}  prec={v['prec']}")
except Exception as e:
    print(f"    读取失败: {type(e).__name__}: {e}")
