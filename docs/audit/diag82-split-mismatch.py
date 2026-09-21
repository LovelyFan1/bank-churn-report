"""核实：两处 train_test_split 的划分是否真的不同（导致阈值不一致）。

risk_scoring._ensure_engine      : train_test_split(idx, y, ...)     → 对索引划分
cost_benefit.analyze_thresholds  : train_test_split(X, y, ...)       → 对特征矩阵划分

两者 random_state 相同，但 sklearn 的划分依据是**输入的样本数与前几个维度的
hash**，输入不同则结果不同。若两者测试集不同，则在各自集合上选的阈值就可能不同。
"""
import numpy as np
from sklearn.model_selection import train_test_split

n = 96418
rng = np.random.default_rng(0)
y = (rng.random(n) < 0.204).astype(int)
idx = np.arange(n)
X = rng.random((n, 3))

# 方式 A：对索引划分（risk_scoring 用）
a_tr, a_te = train_test_split(idx, test_size=0.2, random_state=42, stratify=y)

# 方式 B：对 X 划分（cost_benefit 用）
#   注意 sklearn 返回的是 X 的**行**，需要还原成行号才能与 A 比较 ——
#   这正是两处实现"看起来都在划分、实际划的不是一回事"的根源。
b_tr, b_te, _, _ = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
# 用第一列的唯一值反查行号（构造时 X 每行首列唯一）
row_of = {float(v): i for i, v in enumerate(X[:, 0])}
b_te_idx = {row_of[float(r[0])] for r in b_te}

sa = set(a_te.tolist())
sb = b_te_idx
print(f"方式A 测试集 {len(sa):,}   方式B 测试集 {len(sb):,}")
print(f"两者交集 {len(sa & sb):,}")
print(f"仅 A 有   {len(sa - sb):,}")
print(f"仅 B 有   {len(sb - sa):,}")
print(f"完全一致？ {sa == sb}")
print()
if sa != sb:
    print("→ 划分不同 ⇒ 在各自测试集上算出的指标不同 ⇒ 选出的最优阈值可能不同")
    print("  这就是 /api/model/risk-info(0.60) 与 /api/cost-benefit/summary(0.65)")
    print("  两个阈值打架的根因。")
else:
    print("→ 划分相同，阈值差异另有原因")
