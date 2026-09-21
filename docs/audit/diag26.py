"""第二十六轮：验证 predict.py 的两个疑点。

疑点 1：_load_best_model 无 try/except —— meta.json 正被训练写入时会抛异常
        （对比 risk_scoring._load_best_model 有 3 次重试）
疑点 2：_enrich_with_shap 里 `FEATURE_NAMES[i]` 与 shap_vals[i] 的对应
        —— FEATURE_NAMES 有 12 个，features_matrix 应也是 12 列，需确认
疑点 3：SHAP 贡献度写成 `v * 100:.1f}%` —— 若 v 是 log-odds 或原始
        shap 值，乘 100 当百分比是**误导性**的（各特征加起来不等于 100%）
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, json
from app.services.data_loader import FEATURE_NAMES, NUMERIC_FEATURES, CATEGORICAL_FEATURES

print("=" * 92)
print("一、FEATURE_NAMES 与特征矩阵列数")
print("=" * 92)
print(f"  NUMERIC_FEATURES   ({len(NUMERIC_FEATURES)}): {NUMERIC_FEATURES}")
print(f"  CATEGORICAL_FEATURES({len(CATEGORICAL_FEATURES)}): {CATEGORICAL_FEATURES}")
print(f"  FEATURE_NAMES      ({len(FEATURE_NAMES)}): {FEATURE_NAMES}")

from app.database import SessionLocal
from app.services.data_loader import prepare_features, get_cached_customer_df
db = SessionLocal()
df = get_cached_customer_df(db)
X, y, fn = prepare_features(df)
print(f"\n  prepare_features → X.shape = {X.shape}")
print(f"  len(FEATURE_NAMES) = {len(FEATURE_NAMES)}")
print(f"  → {'✅ 匹配' if X.shape[1] == len(FEATURE_NAMES) else '❌ 不匹配！SHAP 归因会错位'}")

print()
print("=" * 92)
print("二、SHAP 贡献度的百分比是否成立（核心疑点）")
print("=" * 92)
import joblib
from pathlib import Path
meta = json.load(open("/app/saved_models/meta.json", encoding="utf-8"))
best = max(meta["results"], key=lambda k: meta["results"][k]["auc"])
mp = Path("/app/saved_models") / (best.lower().replace(" ", "_") + ".joblib")
model = joblib.load(mp)
print(f"  best model = {best}")

import shap
N = 50
Xsub = X[:N]
print(f"  shap 版本 = {shap.__version__}")

# ⚠ 复刻生产代码的写法，看是否抛异常
print(f"\n  ── 复刻生产写法 shap.TreeExplainer(model, model_output='probability') ──")
try:
    explainer = shap.TreeExplainer(model, model_output="probability")
    sv = explainer.shap_values(Xsub)
    print(f"    ✅ 成功, sv.shape = {np.shape(sv)}")
except Exception as e:
    print(f"    ❌ 抛异常: {type(e).__name__}: {e}")
    print(f"    → 生产代码 _enrich_with_shap 的 except 会**静默吞掉**，")
    print(f"      降级为 _risk_factors_from_row（硬阈值规则）")
    sv = None

print(f"\n  ── 改用 model_output='raw' ──")
try:
    ex2 = shap.TreeExplainer(model, model_output="raw")
    sv2 = ex2.shap_values(Xsub)
    print(f"    ✅ 成功, sv.shape = {np.shape(sv2)}")
    sv = sv2[1] if isinstance(sv2, list) else sv2
    base = ex2.expected_value
    if isinstance(base, (list, np.ndarray)):
        base = base[1] if len(np.atleast_1d(base)) > 1 else float(np.atleast_1d(base)[0])
    print(f"    base_value = {base}")
except Exception as e:
    print(f"    ❌ {type(e).__name__}: {e}")
    sv = None

if sv is None:
    print("\n  ⚠ 两种写法都失败，跳过后续 SHAP 分析")
    db.close()
    raise SystemExit(0)

# 现在检验「把 shap 值 × 100 当百分比」是否合理
print()
print("=" * 92)
print("三、贡献度百分比的实际数值分布")
print("=" * 92)
probs = model.predict_proba(Xsub)[:, 1]
row_sums = sv.sum(axis=1)
print(f"  可加性检验: max|prob - (base+sum)| = "
      f"{float(np.abs(probs - (base + row_sums)).max()):.6f}")
print(f"  ⚠ 注意：这里用的是 model_output='raw'，即 **log-odds 尺度**，")
print(f"     不是概率尺度。所以 shap 值之和 ≠ 概率差。")
print("     生产代码却把它写成 `v * 100` 加百分号 —— 需要判断这有多误导。\n")

pairs_all = []
for i in range(N):
    pairs = [(FEATURE_NAMES[j], float(sv[i][j])) for j in range(len(FEATURE_NAMES))]
    pairs.sort(key=lambda x: abs(x[1]), reverse=True)
    top6 = [p for p in pairs[:6] if abs(p[1]) > 0.003]
    pct = [v * 100 for _, v in top6]
    pairs_all.append(pct)

flat = [x for p in pairs_all for x in p]
if flat:
    print(f"  展示的百分比值: min={min(flat):.2f}%  max={max(flat):.2f}%  "
          f"mean={np.mean(flat):.2f}%")
    print(f"  出现 > 100% 的条目数: {sum(1 for x in flat if abs(x) > 100)}")
    print(f"  出现 > 50% 的条目数: {sum(1 for x in flat if abs(x) > 50)}")
    print()
    print("  ⚠ 若这些值合计不等于 100%，把单个特征写成 'xx.x%' 会让用户")
    print("     误以为它们是占比。实测每行 top6 百分比之和:")
    sums = [sum(abs(x) for x in p) for p in pairs_all if p]
    print(f"       min={min(sums):.1f}%  max={max(sums):.1f}%  mean={np.mean(sums):.1f}%")
    print(f"     → 若远大于 100%，说明这不是占比，而是**绝对贡献度**")

# 更严谨：负值意味着"降低流失概率"，百分比写法会误导
neg = sum(1 for x in flat if x < 0)
print(f"\n  负值条目（降低流失概率的特征）: {neg}/{len(flat)} ({neg/len(flat)*100:.1f}%)")
print(f"  → 前端会显示成 '-12.3%'，容易被理解为'降低 12.3% 的流失率'，")
print(f"     实际是 SHAP 贡献度的相对大小")

print()
print("=" * 92)
print("四、_load_best_model 健壮性（predict.py 版 vs risk_scoring 版）")
print("=" * 92)
import inspect
from app.celery_tasks import predict as pt
src = inspect.getsource(pt._load_best_model)
print(f"  predict.py._load_best_model 是否含 try: {'try' in src}")
print(f"  是否含重试循环: {'range(' in src}")
from app.services import risk_scoring as rs
src2 = inspect.getsource(rs._load_best_model)
print(f"  risk_scoring._load_best_model 是否含 try: {'try' in src2}")
print(f"  重试次数: 3 (ATTEMPTS)")

# ⚠ 绝不触碰生产 meta.json。改用临时目录 + monkeypatch MODEL_DIR。
import os, shutil, tempfile
tmpdir = tempfile.mkdtemp(prefix="meta_test_")
fake_meta = os.path.join(tmpdir, "meta.json")
prod_meta = "/app/saved_models/meta.json"
prod_size = os.path.getsize(prod_meta)
shutil.copyfile(prod_meta, fake_meta)
with open(fake_meta, "wb") as f:
    f.write(open(prod_meta, "rb").read()[: prod_size // 2])
print(f"\n  在临时目录构造半截 meta.json: {fake_meta} ({os.path.getsize(fake_meta)} 字节)")
print(f"  生产 meta.json 未被触碰: {os.path.getsize(prod_meta) == prod_size}")

from pathlib import Path as _P
# predict.py 版：把模块级 MODEL_DIR 指向临时目录
orig_pt_dir = pt.MODEL_DIR
pt.MODEL_DIR = _P(tmpdir)
try:
    r = pt._load_best_model()
    print(f"    predict.py   → 返回 ({r[0] is not None}, {r[1]})")
except Exception as e:
    print(f"    predict.py   → ❌ 抛异常 {type(e).__name__}: {str(e)[:90]}")
finally:
    pt.MODEL_DIR = orig_pt_dir

# risk_scoring 版
orig_rs_dir = rs.MODEL_DIR
rs.MODEL_DIR = _P(tmpdir)
rs._best_model_cache["model"] = None; rs._best_model_cache["ts"] = 0.0
try:
    r2 = rs._load_best_model()
    print(f"    risk_scoring → 返回 ({r2[0] is not None}, {r2[1]})")
except Exception as e:
    print(f"    risk_scoring → ❌ 抛异常 {type(e).__name__}: {str(e)[:90]}")
finally:
    rs.MODEL_DIR = orig_rs_dir
    shutil.rmtree(tmpdir, ignore_errors=True)

print(f"\n  生产 meta.json 仍然完好: {os.path.getsize(prod_meta) == prod_size}")

db.close()
