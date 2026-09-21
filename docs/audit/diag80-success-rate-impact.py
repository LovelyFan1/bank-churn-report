"""测算：把「挽留成功率 s」引入成本模型后，最优阈值会怎么变。

背景
----
当前 net_profit（单位 = 一次干预成本 c）：
    TP → +(cost_ratio - 1)     即「劝住一个」拿全额客户价值
    FP → -1
    FN → 0（与"不干预"基准相比无增量）
隐含 s = 100%。

引入 s 后，一个 TP 的**期望**收益 = s×客户价值 - c，
换算成 c 的单位 = s×cost_ratio - 1：
    net_profit(t) = TP×(s×cost_ratio - 1) - FP

⚠ 关键后果：s 越低，单个 TP 的奖励越小，就需要**更高的精度**才划算
   —— 最优阈值必然上移。因此「阈值 0.2」这个结论本身是 s=1 假设的产物。

本脚本在真实数据上测算每个 s 对应的最优阈值与指标，供定档参考。
"""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

MODEL_DIR = Path("/app/saved_models")
COST_RATIO = 5.0

meta = json.load(open(MODEL_DIR / "meta.json", encoding="utf-8"))
best = max(meta["results"].keys(), key=lambda k: meta["results"][k]["auc"])
model = joblib.load(MODEL_DIR / (best.lower().replace(" ", "_") + ".joblib"))
print(f"最佳模型 = {best}")

# 复现 prepare_features 的列顺序
import sys
sys.path.insert(0, "/app")
from app.database import SessionLocal
from app.services.data_loader import prepare_features, get_cached_customer_df

db = SessionLocal()
df = get_cached_customer_df(db)
X, y, names = prepare_features(df)
print(f"样本 = {len(df):,}   特征 = {len(names)}")

idx = np.arange(len(df))
train_idx, test_idx = train_test_split(
    idx, test_size=0.2, random_state=42, stratify=y)
print(f"训练集 = {len(train_idx):,}   测试集 = {len(test_idx):,}\n")

raw = model.predict_proba(X)[:, 1]


def best_threshold(s, on="train"):
    """在指定集合上选使 net_profit 最大的阈值。"""
    sel = train_idx if on == "train" else test_idx
    p, yy = raw[sel], y[sel]
    grid = np.arange(0.05, 0.96, 0.05)
    best_t, best_v = 0.5, -1e18
    for t in grid:
        pred = (p >= t).astype(int)
        tp = int(((pred == 1) & (yy == 1)).sum())
        fp = int(((pred == 1) & (yy == 0)).sum())
        v = tp * (COST_RATIO * s - 1) - fp
        if v > best_v:
            best_v, best_t = v, float(t)
    return best_t


def metrics_at(t):
    p, yy = raw[test_idx], y[test_idx]
    pred = (p >= t).astype(int)
    tp = int(((pred == 1) & (yy == 1)).sum())
    fp = int(((pred == 1) & (yy == 0)).sum())
    fn = int(((pred == 0) & (yy == 1)).sum())
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    return tp, fp, fn, prec, rec, (tp + fp) / len(yy)


print("=" * 96)
print("挽留成功率 s → 最优阈值 / 精度 / 召回 / 覆盖率 / ROI")
print("=" * 96)
print(f"{'s':>6} {'最优阈值':>9} {'精度':>8} {'召回':>8} {'覆盖率':>8} {'触达人数':>10} "
      f"{'ROI':>7}  {'ROI>1?':>7}")
print("-" * 96)

for s in [1.00, 0.80, 0.70, 0.65, 0.60, 0.50, 0.40, 0.35, 0.30, 0.25, 0.20]:
    t = best_threshold(s)
    tp, fp, fn, prec, rec, cov = metrics_at(t)
    roi = COST_RATIO * prec * s
    n_flagged = int(cov * len(df))
    print(f"{s:>6.2f} {t:>9.2f} {prec:>8.4f} {rec:>8.4f} {cov*100:>7.2f}% "
          f"{n_flagged:>10,} {roi:>7.2f}  {'是' if roi > 1 else '否':>7}")

print()
print("=" * 96)
print("对照：固定各阈值，看 ROI 随 s 的变化（检验'阈值该不该动'）")
print("=" * 96)
print(f"{'阈值':>6} {'精度':>8} {'覆盖率':>8} " + "".join(f"{f's={s}':>9}" for s in [1.0, 0.5, 0.35, 0.30]))
print("-" * 96)
for t in [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
    tp, fp, fn, prec, rec, cov = metrics_at(t)
    rois = "".join(f"{COST_RATIO*prec*s:>9.2f}" for s in [1.0, 0.5, 0.35, 0.30])
    print(f"{t:>6.2f} {prec:>8.4f} {cov*100:>7.2f}% {rois}")

print()
print("=" * 96)
print("盈亏平衡所需的精度（precision > 1 / (cost_ratio × s)）")
print("=" * 96)
for s in [1.0, 0.65, 0.50, 0.40, 0.35, 0.30, 0.25]:
    need = 1 / (COST_RATIO * s)
    # 找出满足该精度的最低阈值
    hit = None
    for t in np.arange(0.50, 0.96, 0.01):
        _, _, _, prec, _, _ = metrics_at(round(float(t), 2))
        if prec >= need:
            hit = round(float(t), 2)
            break
    print(f"  s={s:.2f}  需精度 > {need:.4f}   "
          f"满足该精度的阈值 ≈ {hit if hit is not None else '无（即使 0.95 也不够）'}")

db.close()
