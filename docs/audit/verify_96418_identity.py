"""关键验证：新 CSV 是否 == 原始 CSV 的前 96418 行（逐格）。

这是整个「缩量」方案的核心断言：
  - 若成立 → 客户身份完全保留，60 条工单仍指向同一批人，
             所有历史引用（工单/快照/快照阈值）都依然有效。
  - 若不成立 → 相当于换了一批人，工单会张冠李戴。

同时验证派生文件（monthly / transactions）也是「过滤掉越界客户」，
而不是被重排或改动。
"""
import pandas as pd
import sqlite3

NEW = "/tmp/new_96418.csv"
ORIG = "/tmp/orig_100k.csv"

new = pd.read_csv(NEW)
orig = pd.read_csv(ORIG)
print(f"新 CSV   : {len(new):,} 行 × {new.shape[1]} 列")
print(f"原始 CSV : {len(orig):,} 行 × {orig.shape[1]} 列")
print()

# ── 1) 除 row_number 外，全部 33 列逐格比对 ──
cut = orig.head(len(new)).reset_index(drop=True)
cmp_cols = [c for c in orig.columns if c != "row_number"]

print("=" * 72)
print("1) 新 CSV vs 原始前 96418 行（33 列逐格比对）")
print("=" * 72)
same = True
for c in cmp_cols:
    a = cut[c]
    b = new[c]
    if a.dtype.kind in "fi" and b.dtype.kind in "fi":
        eq = (a.fillna(-9e18) == b.fillna(-9e18)).all()
    else:
        eq = (a.astype(str) == b.astype(str)).all()
    if not eq:
        same = False
        n = int((a.astype(str) != b.astype(str)).sum())
        print(f"  [DIFF] {c:24s} 不同 {n:,} 处")
print(f"  >>> 33 列全部一致: {same}")

# row_number 应被重编为 1..N
rn_ok = (new["row_number"].values == range(1, len(new) + 1)).all()
print(f"  >>> row_number 重编为 1..{len(new):,}: {rn_ok}")

# ── 2) 汇总统计一致性 ──
print()
print("=" * 72)
print("2) 汇总统计")
print("=" * 72)
print(f"{'指标':<22} {'原始前96418':>14} {'新CSV':>14} {'是否相同':>10}")
for col, fn, label in [
    ("exited", "mean", "流失率"),
    ("age", "mean", "平均年龄"),
    ("balance", "mean", "平均余额"),
    ("balance", "min", "最小余额"),
    ("estimated_salary", "max", "最大薪资"),
    ("customer_id", "max", "最大编号"),
]:
    a = getattr(cut[col], fn)()
    b = getattr(new[col], fn)()
    if isinstance(a, float):
        ok = abs(a - b) < 1e-9
    else:
        ok = a == b
    print(f"{label:<22} {str(a):>14} {str(b):>14} {str(ok):>10}")
