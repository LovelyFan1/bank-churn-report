"""E04 回归 —— SQLite 运行口径与并发（隔离临时库，不碰正式库）。

结论先行：本次不切换 WAL。理由与部署步骤见
`docs/SQLITE_CONCURRENCY_2026-10-23.md`：容器内 Python 实际加载
SQLite 3.46.1，低于官方 WAL-reset 缺陷的修复版本，且没有证据表明
Debian 包已回补。因此这里断言的是"现有模式 + 连接级等待上限"的正确性。
"""
import multiprocessing as mp
import os
import sqlite3
import tempfile
import time
import unittest

from sqlalchemy import create_engine, event, text

from app import database

WRITERS = 3
ROWS_PER_WRITER = 30


def _writer(path: str, index: int, barrier) -> None:
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        barrier.wait(timeout=30)
        for row in range(ROWS_PER_WRITER):
            for _attempt in range(200):
                try:
                    conn.execute(
                        "insert into probe(writer, seq, ts) values (?, ?, ?)",
                        (index, row, time.time()))
                    conn.commit()
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower():
                        raise
                    time.sleep(0.02)
            else:                              # pragma: no cover - 仅在持续锁死时触发
                raise AssertionError("持续锁等待后仍未写入")
    finally:
        conn.close()


class RuntimeFacts(unittest.TestCase):
    def test_runtime_reports_actual_library_and_timeout(self):
        """E04-T02：报告实际加载的 SQLite 版本与连接级等待上限。"""
        info = database.sqlite_runtime_info()
        self.assertTrue(info["is_sqlite"])
        self.assertIn("sqlite_version", info)
        self.assertEqual(int(info["busy_timeout_ms"]), database.BUSY_TIMEOUT_MS)

    def test_every_new_connection_gets_the_same_busy_timeout(self):
        """E04-T02：等待上限是连接级行为，不是一次初始化就全生效。"""
        with database.engine.connect() as a, database.engine.connect() as b:
            self.assertEqual(a.execute(text("PRAGMA busy_timeout")).scalar(),
                             database.BUSY_TIMEOUT_MS)
            self.assertEqual(b.execute(text("PRAGMA busy_timeout")).scalar(),
                             database.BUSY_TIMEOUT_MS)

    def test_journal_mode_is_not_switched_per_connection(self):
        """E04-T01：不为切并发而改动 journal_mode（WAL 未获版本证明）。"""
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        engine = create_engine(f"sqlite:///{path}",
                               connect_args={"check_same_thread": False, "timeout": 30})
        event.listen(engine, "connect", database._sqlite_pragmas)
        self.addCleanup(engine.dispose)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("PRAGMA busy_timeout")).scalar(),
                             database.BUSY_TIMEOUT_MS)
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        self.assertEqual(str(mode).lower(), "delete")


class ConcurrentWriters(unittest.TestCase):
    def test_multiprocess_writes_are_complete_and_consistent(self):
        """E04-T03/T07（隔离版）：多进程写同一文件库——全部提交、结构完好。"""
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        conn = sqlite3.connect(path)
        conn.execute("create table probe(id integer primary key autoincrement, "
                     "writer int, seq int, ts real)")
        conn.commit()
        conn.close()

        ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context("spawn")
        barrier = ctx.Barrier(WRITERS)
        procs = [ctx.Process(target=_writer, args=(path, i, barrier))
                 for i in range(WRITERS)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=90)
        self.assertTrue(all(proc.exitcode == 0 for proc in procs),
                        [proc.exitcode for proc in procs])

        check = sqlite3.connect(path)
        try:
            total = check.execute("select count(*) from probe").fetchone()[0]
            writers = check.execute("select count(distinct writer) from probe").fetchone()[0]
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            journal = check.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            check.close()
        self.assertEqual(total, WRITERS * ROWS_PER_WRITER)
        self.assertEqual(writers, WRITERS)
        self.assertEqual(integrity, "ok")
        self.assertEqual(str(journal).lower(), "delete")


if __name__ == "__main__":
    unittest.main()
