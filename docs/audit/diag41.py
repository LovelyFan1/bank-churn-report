"""第四十一轮：核实首页「关键发现」的文案与数字是否自洽。只读。

⚠ 观察到的疑点（页面原文）：
    "🔴 投诉客户流失率极高  20.4%  是总体的 1.0 倍"
    —— "极高"与"1.0 倍"自相矛盾。20.4% 恰好等于总体流失率 20.42%，
       即"投诉客户的流失率与总体**完全相同**"，谈不上"极高"。
    "⚪ 零余额客户流失率  13.8%  是有余额的 0.6 倍"
    —— 零余额反而更低，也不该作为"风险"列出。
"""
import json, urllib.request
import numpy as np
from app.database import SessionLocal
from app.services.data_loader import get_cached_customer_df

def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

db = SessionLocal()
df = get_cached_customer_df(db)
base = df["exited"].mean() * 100

print("=" * 92)
print("一、计算各分组的真实流失率")
print("=" * 92)
print(f"  总体流失率 = {base:.2f}%\n")

groups = {
    "complain==1 (投诉客户)": df[df["complain"] == 1],
    "complain==0 (无投诉)":   df[df["complain"] == 0],
    "num_products>=3":       df[df["num_products"] >= 3],
    "num_products==1":       df[df["num_products"] == 1],
    "Germany":               df[df["geography"] == "Germany"],
    "France":                df[df["geography"] == "France"],
    "is_active==1":          df[df["is_active_member"] == 1],
    "is_active==0":          df[df["is_active_member"] == 0],
    "age>=50":               df[df["age"] >= 50],
    "age<50":                df[df["age"] < 50],
    "balance==0":            df[df["balance"] == 0],
    "balance>0":             df[df["balance"] > 0],
}
stats = {}
for name, g in groups.items():
    r = g["exited"].mean() * 100 if len(g) else float("nan")
    stats[name] = r
    print(f"  {name:26s} n={len(g):6d}  流失率={r:6.2f}%  "
          f"相对总体={r/base:5.2f}x")

print()
print("=" * 92)
print("二、接口 /api/eda/key-insights 返回的内容")
print("=" * 92)
ki = get("/api/eda/key-insights")
print(json.dumps(ki, ensure_ascii=False, indent=2)[:2500])

db.close()
