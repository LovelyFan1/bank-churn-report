"""第二十七轮：SHAP factors 与 reason 的一致性冲突频率。

⚠ 背景：修复 SHAP 后，risk_factors 变成带占比的 SHAP 标签（按贡献降序）。
   而 reason 由 recommend_action 按 _REASON_MAP 的**固定顺序**匹配。
   两者顺序不同 → 可能出现「列表首项是A(75%)，reason 却说B」的不自洽。
   本脚本量化冲突频率，据此决定是否需要改 reason 逻辑（避免过度改动）。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, re
from app.database import SessionLocal
from app.services import risk_scoring as rs
from app.celery_tasks import predict as pt
from app.services.data_loader import get_cached_customer_df, prepare_features
import joblib
from pathlib import Path

db = SessionLocal()
df = get_cached_customer_df(db)
X, y, _ = prepare_features(df)
meta = __import__("json").load(open("/app/saved_models/meta.json", encoding="utf-8"))
best = max(meta["results"], key=lambda k: meta["results"][k]["auc"])
model = joblib.load(Path("/app/saved_models") / (best.lower().replace(" ", "_") + ".joblib"))
print(f"model = {best}")

# 取一个较大的样本做统计（用 Top 2000 高概率客户）
probs = model.predict_proba(X)[:, 1]
top_idx = np.argsort(-probs)[:2000]
print(f"样本: 概率最高的 2000 个客户")

import shap
ex = shap.TreeExplainer(model)
sv = ex.shap_values(X[top_idx])
if isinstance(sv, list):
    sv = sv[1]
sv = np.asarray(sv)
print(f"SHAP shape = {sv.shape}")

print()
print("=" * 92)
print("一、SHAP 各特征进入 top 的频次（看哪些特征常被当'风险因素'）")
print("=" * 92)
from app.services.data_loader import FEATURE_NAMES
# 每行 top1 特征
top1 = np.argmax(sv, axis=1)      # 最大正贡献
import collections
c = collections.Counter(top1.tolist())
print("  每行「最大正贡献特征」的分布:")
for i, n in c.most_common():
    print(f"    {FEATURE_NAMES[i]:22s} {n:5d} 次 ({n/len(top1)*100:5.1f}%)")

print()
print("=" * 92)
print("二、用当前生产逻辑，量化「factors 首项」与「reason」的不自洽率")
print("=" * 92)
LABELS = pt.SHAP_FEATURE_LABELS
conflicts = []
for i in range(len(top_idx)):
    row = df.iloc[top_idx[i]]
    vals = sv[i]
    pairs = [(FEATURE_NAMES[j], float(vals[j])) for j in range(len(FEATURE_NAMES))]
    pos = [(f, v) for f, v in pairs if v > 0]
    if not pos:
        continue
    pos.sort(key=lambda x: x[1], reverse=True)
    total = sum(v for _f, v in pos)
    factors = [f"{LABELS.get(f, f)}（{v/total*100:.1f}%）" for f, v in pos[:6]]

    rec = {"customer_id": str(row["customer_id"]), "balance": float(row["balance"]),
           "probability": float(probs[top_idx[i]]),
           "risk_level": rs._level(float(probs[top_idx[i]]), rs._ensure_engine(db)["thresholds"]),
           "value_tier": rs.value_tier(float(row["balance"])),
           "risk_factors": factors}
    pt._apply_strategy(rec)
    reason = rec["reason"]
    # 哪个因素产生了这个 reason？
    src = None
    for f in factors:
        for keywords, text in rs._REASON_MAP:
            if any(kw in f for kw in keywords):
                if text == reason:
                    src = f
                break
        if src:
            break
    first = factors[0]
    # 首项是否就是 reason 的来源
    first_is_src = any(
        any(kw in first for kw in keywords) and text == reason
        for keywords, text in rs._REASON_MAP
    )
    if not first_is_src:
        conflicts.append((rec["customer_id"], first, reason, factors[:3]))

print(f"  样本数 = {len(top_idx)}")
print(f"  「factors 首项 ≠ reason 来源」的次数 = {len(conflicts)} "
      f"({len(conflicts)/len(top_idx)*100:.1f}%)")
print()
print("  前 10 例:")
for cid, first, reason, f3 in conflicts[:10]:
    print(f"    {cid}: 首项={first}")
    print(f"           reason={reason}")
    print(f"           top3={f3}")

print()
print("=" * 92)
print("三、若改为「按 factors 顺序取首个匹配」，会改变多少客户的 reason")
print("=" * 92)
changed = 0
for i in range(len(top_idx)):
    row = df.iloc[top_idx[i]]
    vals = sv[i]
    pairs = [(FEATURE_NAMES[j], float(vals[j])) for j in range(len(FEATURE_NAMES))]
    pos = [(f, v) for f, v in pairs if v > 0]
    if not pos:
        continue
    pos.sort(key=lambda x: x[1], reverse=True)
    total = sum(v for _f, v in pos)
    factors = [f"{LABELS.get(f, f)}（{v/total*100:.1f}%）" for f, v in pos[:6]]
    # 现状
    old = rs.recommend_action(None, "LOW", factors)["reason"]
    # 新：按 factors 顺序
    new = "常规维护建议"
    for f in factors:
        hit = None
        for keywords, text in rs._REASON_MAP:
            if any(kw in f for kw in keywords):
                hit = text
                break
        if hit:
            new = hit
            break
    if old != new:
        changed += 1
print(f"  reason 会变化的客户数 = {changed}/{len(top_idx)} ({changed/len(top_idx)*100:.1f}%)")
print()
print("  ⚠ 这些变化**只影响文案**，不影响 risk_level / strategy / channel / 数值。")

print()
print("=" * 92)
print("四、关键：当前 SHAP 标签能否被 _REASON_MAP 匹配上")
print("=" * 92)
print("  SHAP_FEATURE_LABELS 与 _REASON_MAP 关键词的匹配情况:")
for feat, label in LABELS.items():
    matched = [text for keywords, text in rs._REASON_MAP
               if any(kw in label for kw in keywords)]
    flag = "✅" if matched else "❌ 无法匹配（reason 会跳过它）"
    print(f"    {feat:22s} → 「{label:12s}」 {flag}")

db.close()
