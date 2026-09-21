"""第二十一轮：核实「前 N 行」vs「随机 N 行」的抽样偏差（肘部法则与 silhouette 都用了前者）。

⚠ 使用只读沙箱，不碰生产库。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np
import pandas as pd
from sandbox import session
from sqlalchemy import text

db = session()
print("=" * 92)
print("一、确认连的是沙箱而非生产库")
print("=" * 92)
url = str(db.get_bind().url)
print(f"  engine url = {url}")
assert "audit" in url or "/tmp" in url, "⛔ 不是沙箱连接"
print("  ✅ 沙箱确认")

rows = db.execute(text(
    "SELECT id, credit_score, age, tenure, balance, num_products, "
    "has_credit_card, is_active_member, estimated_salary, exited, "
    "satisfaction_score, points_earned, balance_salary_ratio, cluster_id "
    "FROM customers ORDER BY id")).fetchall()
db.close()
cols = ["id", "credit_score", "age", "tenure", "balance", "num_products",
        "has_credit_card", "is_active_member", "estimated_salary", "exited",
        "satisfaction_score", "points_earned", "balance_salary_ratio", "cluster_id"]
df = pd.DataFrame(rows, columns=cols)
print(f"  读入 {len(df)} 行")

first5k = df.iloc[:5000]
rng = np.random.default_rng(42)
ridx = rng.choice(len(df), 5000, replace=False)
rand5k = df.iloc[ridx]

print()
print("=" * 92)
print("二、前 5000 行 vs 随机 5000 行 vs 全量 —— 各维度对比")
print("=" * 92)
feats = ["credit_score","age","tenure","balance","num_products","has_credit_card",
         "is_active_member","estimated_salary","satisfaction_score","points_earned",
         "balance_salary_ratio"]
print(f"{'字段':>22} {'前5000':>12} {'随机5000':>12} {'全量':>12} {'前5000偏差%':>12}")
for f in feats:
    a = first5k[f].mean(); b = rand5k[f].mean(); c = df[f].mean()
    dev = (a - c) / c * 100 if c else 0
    flag = "  ⚠" if abs(dev) > 5 else ""
    print(f"{f:>22} {a:>12,.2f} {b:>12,.2f} {c:>12,.2f} {dev:>11.2f}%{flag}")

print()
print(f"{'流失率':>22} {first5k['exited'].mean()*100:>11.2f}% "
      f"{rand5k['exited'].mean()*100:>11.2f}% {df['exited'].mean()*100:>11.2f}%")

print()
print("=" * 92)
print("三、关键：小簇（低薪客群）在两种抽样里的代表性")
print("=" * 92)
low = (df["estimated_salary"] < 5000)
print(f"  全量 salary<5000 人数: {low.sum()}")
print(f"  前 5000 行中: {low.iloc[:5000].sum()}")
print(f"  随机 5000 行中: {low.iloc[ridx].sum()}")

low2 = (df["estimated_salary"] < 10000)
print(f"\n  全量 salary<10000 人数: {low2.sum()}")
print(f"  前 5000 行中: {low2.iloc[:5000].sum()}")
print(f"  随机 5000 行中: {low2.iloc[ridx].sum()}")

print()
print("=" * 92)
print("四、data_source.py 只保留 Customer 认识的列 —— 核实丢了什么")
print("=" * 92)
import os
csv = "/data-source/customers_simulated.csv"
if os.path.exists(csv):
    import csv as _csv
    with open(csv, newline="", encoding="utf-8") as f:
        rd = _csv.reader(f); header = next(rd)
    print(f"  CSV 列数 = {len(header)}")
    print(f"  CSV 列名 = {header}")
    known = set(cols) - {"id"}
    missing = [c for c in header if c not in known]
    print(f"\n  Customer 模型认识的列 ({len([c for c in header if c in known])}):")
    print(f"    {[c for c in header if c in known]}")
    print(f"\n  ⚠ 被丢弃的列 ({len(missing)}):")
    print(f"    {missing}")
else:
    print(f"  CSV 不存在: {csv}")
