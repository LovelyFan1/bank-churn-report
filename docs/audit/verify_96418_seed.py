"""核验：数据库与截取后的 CSV 是否逐行一致（抽样 + 聚合双层校验）。

用途：确认「清表 → 重灌」没有引入任何偏差。
"""
import sqlite3

import pandas as pd

CSV = r"D:\创新创业\创新创业\模拟仿真客户数据\customers_simulated.csv"
DB = r"\\?\pipe"  # 占位，实际在容器内跑

df = pd.read_csv(CSV)

# 容器内导出一份 DB 数据供本地比对
import subprocess
out = subprocess.run(
    ["docker", "compose", "exec", "-T", "backend", "python", "-c",
     "import sqlite3,json;"
     "c=sqlite3.connect('/app/data/churn_analysis.db');"
     "rows=c.execute('SELECT customer_id,age,balance,exited,num_products,geography,"
     "estimated_salary,surname,complain FROM customers ORDER BY id').fetchall();"
     "print(json.dumps(rows))"],
    cwd=r"D:\创新创业\创新创业\bank-churn-report",
    capture_output=True, text=True, encoding="utf-8",
)
if out.returncode != 0:
    print("导出失败:", out.stderr[-2000:])
    raise SystemExit(1)

import json
rows = json.loads(out.stdout.strip().splitlines()[-1])
db = pd.DataFrame(rows, columns=[
    "customer_id", "age", "balance", "exited", "num_products",
    "geography", "estimated_salary", "surname", "complain"])

print(f"CSV 行数 = {len(df):,}")
print(f"DB  行数 = {len(db):,}")
print(f"行数一致: {len(df) == len(db)}")

csv_part = df[db.columns].reset_index(drop=True)
same = csv_part.equals(db)
print(f"\n>>> 全量逐格比对（{len(db):,} 行 × {db.shape[1]} 列）: {'完全一致' if same else '存在差异'}")

if not same:
    neq = (csv_part != db)
    diff_rows = neq.any(axis=1)
    print(f"    差异行数: {int(diff_rows.sum()):,}")
    for col in db.columns:
        n = int(neq[col].sum())
        if n:
            print(f"    列 {col}: {n:,} 处不同")

print()
print("聚合对照:")
for col, agg in [("exited", "mean"), ("age", "mean"), ("balance", "mean")]:
    a = getattr(df[col], agg)()
    b = getattr(db[col].astype(float), agg)()
    print(f"  {col:18s} CSV={a:.6f}  DB={b:.6f}  diff={abs(a-b):.2e}")
