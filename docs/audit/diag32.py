"""第三十二轮：用各备份比对，定位 work_orders 数量何时从 60 → 59。

⚠ 全程只读打开（file:...?mode=ro），不修改任何备份。
"""
import sqlite3, os, glob

FILES = sorted(glob.glob("/app/data/churn_analysis.db*"))
print("=" * 92)
print("各 db 文件的 work_orders / customers 状态（只读）")
print("=" * 92)
print(f"{'文件':<52} {'mtime':>20} {'orders':>7} {'customers':>10} {'状态分布'}")
for f in FILES:
    if f.endswith(("-wal", "-shm")):
        continue
    name = os.path.basename(f)
    mt = os.path.getmtime(f)
    import datetime
    mts = datetime.datetime.fromtimestamp(mt).strftime("%m-%d %H:%M:%S")
    try:
        con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        cur = con.cursor()
        n_orders = cur.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0]
        n_cust = cur.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        dist = dict(cur.execute(
            "SELECT status, COUNT(*) FROM work_orders GROUP BY status").fetchall())
        con.close()
        print(f"{name:<52} {mts:>20} {n_orders:>7} {n_cust:>10} {dist}")
    except Exception as e:
        print(f"{name:<52} {mts:>20}  ERR {type(e).__name__}: {str(e)[:40]}")

print()
print("=" * 92)
print("结论")
print("=" * 92)
print("""
  · 若 safe-* (04:29:53) 里就是 59，说明变更是**在 04:29:53 之前**发生的，
    那时我尚未启动任何子代理 → 需要重新审视我自己的操作。
  · 若 safe-* 里是 60、而当前 db 是 59，说明是**之后**被改的。
""")

# 对比 cluster_id 是否也被改动
print()
print("=" * 92)
print("cluster_id 分布对比（确认聚类恢复未被破坏）")
print("=" * 92)
for f in FILES:
    if f.endswith(("-wal", "-shm")):
        continue
    name = os.path.basename(f)
    try:
        con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        cur = con.cursor()
        d = dict(cur.execute(
            "SELECT cluster_id, COUNT(*) FROM customers GROUP BY cluster_id").fetchall())
        con.close()
        print(f"  {name:<52} {dict(sorted(d.items(), key=lambda kv: (kv[0] is None, kv[0])))}")
    except Exception as e:
        print(f"  {name:<52} ERR {str(e)[:40]}")
