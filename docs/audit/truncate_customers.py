"""把 customers_simulated.csv 截取到前 N 行（N=96418）。

为什么是「截取」而不是「重新生成」：
    实测 generate_customers(n) **不是前缀稳定的** ——
    --n 3000 与 --n 5000 的前 3000 行逐行全不同（3000/3000 行）。
    所以重新生成会换掉所有客户，60 条既有工单会全部指向不相干的人。
    截取则保留既有客户身份，工单不受影响。

为什么截取是统计安全的：
    生成时各字段为 i.i.d. 采样，行序无结构。实测：
      行号 vs exited 相关系数 = +0.003
      分段流失率 20.32%~20.89%（无趋势）
      前 96418 行 vs 全量的流失率/零余额率/地区占比偏差 < 0.07pp
    即截取等价于均匀随机子样本。

同时重编 row_number 为 1..N（原 row_number 截断后会止于 N，本就连续，
但显式重写可防 CSV 行序与 row_number 不一致时出现空洞）。

用法:
    python truncate_customers.py --n 96418            # 预演，不落盘
    python truncate_customers.py --n 96418 --apply    # 实际写入
"""

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path(r"D:\创新创业\创新创业\模拟仿真客户数据")
CSV = DATA_DIR / "customers_simulated.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    args = ap.parse_args()

    n = args.n
    df = pd.read_csv(CSV)
    total = len(df)
    if n >= total:
        print(f"目标 {n:,} >= 现有 {total:,}，无需截取")
        return 0

    head = df.head(n).copy()
    # row_number 重编为 1..n，保证连续无空洞
    head["row_number"] = range(1, n + 1)

    # customer_id 连续性检查：截取后应恰为 C000001..C{n:06d}
    expect_last = f"C{n:06d}"
    actual_last = head["customer_id"].iloc[-1]
    print(f"截取: {total:,} -> {n:,} 行")
    print(f"  末个 customer_id: {actual_last}  期望 {expect_last}  "
          f"{'OK' if actual_last == expect_last else 'MISMATCH!'}")
    print(f"  customer_id 唯一数: {head['customer_id'].nunique():,}")

    # 统计对照
    def st(d):
        return {
            "churn": d["exited"].mean(),
            "age": d["age"].mean(),
            "zero_bal": (d["balance"] == 0).mean(),
            "geo_DE": (d["geography"] == "Germany").mean(),
        }
    a, b = st(df), st(head)
    print("  统计量对照（偏差）:")
    for k in a:
        print(f"    {k:9s} 全量={a[k]:.4f}  截取后={b[k]:.4f}  "
              f"偏差={((b[k]-a[k])*100):+.3f}pp")

    # 各产品组流失率
    print("  各 num_products 组流失率:")
    for k in (1, 2, 3, 4):
        fa = df[df["num_products"] == k]["exited"].mean()
        fb = head[head["num_products"] == k]["exited"].mean()
        print(f"    np={k}  全量={fa*100:6.2f}%  截取后={fb*100:6.2f}%  "
              f"偏差={(fb-fa)*100:+.2f}pp")

    if not args.apply:
        print("\n[预演模式] 未写入。加 --apply 才会落盘。")
        return 0

    tmp = CSV.with_name(CSV.name + ".tmp")
    head.to_csv(tmp, index=False, encoding="utf-8-sig")
    shutil.move(str(tmp), str(CSV))
    print(f"\n已写入 {CSV}（{n:,} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
