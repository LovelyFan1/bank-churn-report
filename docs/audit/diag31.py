"""第三十一轮：SQLite 并发写风险实测。

⚠ 背景：docker-compose 里 backend 用 `uvicorn --workers 4`，
   Celery worker 另有 2 个并发进程，全部写同一个 SQLite 文件。
   而 create_engine 只设了 check_same_thread=False：
       - 未开启 WAL 模式（默认 journal_mode=delete，写时全库锁）
       - 未设 timeout（默认 5 秒）
       - 未设 busy_timeout
   工单创建/更新是并发写场景，需实测是否会出现 "database is locked"。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import sqlite3, time, threading, os

PROD = "/app/data/churn_analysis.db"

print("=" * 92)
print("一、当前 SQLite 配置（只读查询）")
print("=" * 92)

# ⚠ 用只读连接（mode=ro）查配置，绝不写生产库
con = sqlite3.connect(f"file:{PROD}?mode=ro", uri=True)
cur = con.cursor()
for pragma in ["journal_mode", "busy_timeout", "synchronous", "locking_mode",
               "cache_size", "page_size"]:
    try:
        v = cur.execute(f"PRAGMA {pragma}").fetchone()
        print(f"  PRAGMA {pragma:16s} = {v[0] if v else None}")
    except Exception as e:
        print(f"  PRAGMA {pragma:16s} ERR {e}")
con.close()

print()
print("=" * 92)
print("二、create_engine 的 connect_args")
print("=" * 92)
from app.database import engine
print(f"  URL = {engine.url}")
print(f"  connect_args = {engine.dialect.create_connect_args(engine.url)}")
import inspect
from app import database as dbmod
src = inspect.getsource(dbmod)
print(f"\n  源码:")
for line in src.splitlines()[:12]:
    print(f"    {line}")

print()
print("=" * 92)
print("三、并发写实测（在**沙箱副本**上，不动生产库）")
print("=" * 92)
import shutil
SNAP = "/tmp/audit/readonly_snapshot.db"
CONC = "/tmp/audit/concurrency_test.db"
shutil.copyfile(SNAP, CONC)
print(f"  测试库: {CONC}（生产库的副本）")

# 测 1：默认配置下的并发写
def worker_default(idx, results, n=20):
    try:
        c = sqlite3.connect(CONC, timeout=5)   # 默认 timeout=5s
        for i in range(n):
            c.execute("UPDATE customers SET risk_score = ? WHERE id = ?",
                      (float(idx * 1000 + i), idx + 1))
            c.commit()
        c.close()
        results[idx] = "ok"
    except Exception as e:
        results[idx] = f"{type(e).__name__}: {e}"

results = {}
threads = [threading.Thread(target=worker_default, args=(i, results)) for i in range(8)]
t0 = time.time()
for t in threads: t.start()
for t in threads: t.join()
dt = time.time() - t0
print(f"\n  ── 默认配置（timeout=5s，无 WAL），8 线程 × 每次 commit ──")
print(f"  耗时 {dt:.2f}s")
for k, v in sorted(results.items()):
    mark = "✅" if v == "ok" else "❌"
    print(f"    worker{k}: {mark} {v}")

# 测 2：开启 WAL 后
shutil.copyfile(SNAP, CONC)
def worker_wal(idx, results, n=20):
    try:
        c = sqlite3.connect(CONC, timeout=5)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        for i in range(n):
            c.execute("UPDATE customers SET risk_score = ? WHERE id = ?",
                      (float(idx * 1000 + i), idx + 1))
            c.commit()
        c.close()
        results[idx] = "ok"
    except Exception as e:
        results[idx] = f"{type(e).__name__}: {e}"

results2 = {}
threads = [threading.Thread(target=worker_wal, args=(i, results2)) for i in range(8)]
t0 = time.time()
for t in threads: t.start()
for t in threads: t.join()
dt2 = time.time() - t0
print(f"\n  ── 开启 WAL + busy_timeout，同条件 ──")
print(f"  耗时 {dt2:.2f}s")
for k, v in sorted(results2.items()):
    mark = "✅" if v == "ok" else "❌"
    print(f"    worker{k}: {mark} {v}")

print()
print("=" * 92)
print("四、更极端：长事务持写锁，其他 writer 会怎样")
print("=" * 92)
shutil.copyfile(SNAP, CONC)
c1 = sqlite3.connect(CONC, timeout=0.5)
c1.execute("BEGIN IMMEDIATE")
c1.execute("UPDATE customers SET risk_score = 1 WHERE id = 1")
print("  连接 A: BEGIN IMMEDIATE + UPDATE（持有写锁，未提交）")
try:
    c2 = sqlite3.connect(CONC, timeout=0.5)
    c2.execute("UPDATE customers SET risk_score = 2 WHERE id = 2")
    c2.commit()
    print("  连接 B: ✅ 写入成功")
    c2.close()
except Exception as e:
    print(f"  连接 B: ❌ {type(e).__name__}: {e}")
c1.rollback(); c1.close()

# 清理
for f in [CONC, CONC + "-wal", CONC + "-shm"]:
    if os.path.exists(f):
        os.remove(f)
print("\n  测试文件已清理")

print()
print("=" * 92)
print("五、生产库真实规模与锁竞争面")
print("=" * 92)
print(f"  文件大小: {os.path.getsize(PROD)/1024/1024:.1f} MB")
from app.database import SessionLocal
from app.models.work_order import WorkOrder
from app.models.customer import Customer
db = SessionLocal()
print(f"  customers 行数: {db.query(Customer).count()}")
print(f"  work_orders 行数: {db.query(WorkOrder).count()}")
db.close()
print()
print("  写操作来源：")
print("    · 工单 CRUD（用户操作，频率低但并发可能）")
print("    · 聚类任务 bulk_update_mappings（10 万行，单事务）")
print("    · 批量打分（写成 risk_score，10 万行）")
print("    · 播种（仅首次）")
print("  ⚠ 聚类/批量打分是**长事务写 10 万行**，期间其他写操作会被阻塞")
