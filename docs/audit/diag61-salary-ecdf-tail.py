"""排查 estimated_salary 的 ECDF「第 0 分位点」相对误差。

63.3% 这个数出现在最小值的单一分位点上，需判断是截取引入的问题
还是原本就有的尾部效应。
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

GEN = Path(r"D:\创新创业\创新创业\模拟仿真客户数据\generate_simulated_customers.py")
spec = importlib.util.spec_from_file_location("gen", GEN)
gen = importlib.util.module_from_spec(spec)
sys.modules["gen"] = gen
spec.loader.exec_module(gen)
ref = np.array(gen.ECDF_SALARY)

bk = sorted(Path(r"D:\创新创业\创新创业").glob("_backup-96418-*"))[0]
old = pd.read_csv(bk / "customers_simulated.csv")
new = pd.read_csv(r"D:\创新创业\创新创业\模拟仿真客户数据\customers_simulated.csv")

print("estimated_salary 低端分位对照")
print(f"{'分位%':>7} {'常数表':>12} {'原始100k':>12} {'截取96418':>12}")
for p in [0, 0.1, 0.5, 1, 2, 5, 10, 25, 50, 75, 99, 100]:
    r = float(np.interp(p, np.linspace(0, 100, 101), ref))
    a = float(np.percentile(old.estimated_salary, p))
    b = float(np.percentile(new.estimated_salary, p))
    print(f"{p:>7} {r:>12.2f} {a:>12.2f} {b:>12.2f}")

print()
print("最小值:  原始 =", old.estimated_salary.min(), "  截取 =", new.estimated_salary.min())
print("生成器 clip 下界 lo = 11.58（由 ECDF 表首项给定）")
print("→ 最小值是**离散采样**的结果，不是连续分位数，n 越小越可能取不到表首那个点")

den = np.where(np.abs(ref) < 1e-9, 1.0, np.abs(ref))
q = np.linspace(0, 100, 101)
ro = np.abs(np.percentile(old.estimated_salary, q) - ref) / den
rn = np.abs(np.percentile(new.estimated_salary, q) - ref) / den

print()
print("相对误差 > 3% 的分位点个数:  原始 =", int((ro > 0.03).sum()),
      "  截取 =", int((rn > 0.03).sum()))
print("截取后超标的分位点:", [i for i in range(101) if rn[i] > 0.03])
print("原始   超标的分位点:", [i for i in range(101) if ro[i] > 0.03])

print()
print("排除第 0 分位点（单一最小值）后:")
print(f"  截取后最大相对误差 = {rn[1:].max()*100:.4f}%")
print(f"  原始   最大相对误差 = {ro[1:].max()*100:.4f}%")
