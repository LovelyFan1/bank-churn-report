"""同步截取派生数据文件，使 customer_id 不超出主表范围。

背景：/data-source 下三个文件都含 customer_id：
    customers_simulated.csv        100000 -> 96418
    customer_monthly_snapshot.csv  2400000 行（24 个月 × 10 万客户）
    transactions_sample.csv        308237 行（仅 12500 个客户）

主表截到 96418 后，monthly 里 year_month 对应 C096419..C100000 的行会成为
**孤儿**（引用了不存在的客户）。transactions 最大 C099994，同样越界。

⚠ 这两个文件当前**未被后端任何代码引用**（已核实 backend/ 全库无
   monthly_snapshot / transactions 字样），仅作为扩展数据集存在。
   但它们与主表同属一份数据资产，保持自洽才能经得起交叉验证
   （例如"交易明细里的客户是否都存在于客户主表"这类追问）。

用法:
    python sync_derived_files.py --n 96418            # 预演
    python sync_derived_files.py --n 96418 --apply    # 写入
"""

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd

DATA_DIR = Path(r"D:\创新创业\创新创业\模拟仿真客户数据")
TARGETS = ["customer_monthly_snapshot.csv", "transactions_sample.csv"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    n = args.n
    hi = f"C{n:06d}"   # 主表最大 customer_id
    print(f"主表上界: {hi}（{n:,} 行）\n")

    for name in TARGETS:
        p = DATA_DIR / name
        if not p.exists():
            print(f"[跳过] {name} 不存在")
            continue

        df = pd.read_csv(p)
        before = len(df)
        uniq_before = df["customer_id"].nunique()
        over = df["customer_id"] > hi
        n_over = int(over.sum())

        print(f"=== {name} ===")
        print(f"  行数 {before:,}   唯一客户 {uniq_before:,}")
        print(f"  超出 {hi} 的行: {n_over:,}（{n_over/before*100:.2f}%）")
        if n_over == 0:
            print("  无需处理\n")
            continue

        kept = df[~over].copy()
        print(f"  截取后行数 {len(kept):,}   唯一客户 {kept['customer_id'].nunique():,}")
        print(f"  最大 customer_id: {kept['customer_id'].max()}")

        if not args.apply:
            print("  [预演] 未写入\n")
            continue

        tmp = p.with_name(p.name + ".tmp")
        kept.to_csv(tmp, index=False, encoding="utf-8-sig")
        shutil.move(str(tmp), str(p))
        print(f"  已写入（{len(kept):,} 行）\n")

    if not args.apply:
        print("[预演模式] 未写入。加 --apply 才会落盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
