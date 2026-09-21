"""审计用只读沙箱 —— 强制把实验指向快照副本，绝不动生产库。

用法（在容器内）:
    import sys; sys.path.insert(0, "/tmp/audit")
    from sandbox import session

    db = session()          # 指向 /tmp/audit/readonly_snapshot.db
    ...

设计要点:
  - 快照在容器内 /tmp/audit/readonly_snapshot.db（从生产库复制而来）
  - 引擎用 file:...?mode=ro 打开 —— 即使代码误写也会被 SQLite 拒绝
  - 需要写操作的演练，用 session_writable() 打开**快照的可写副本**，
    绝不指向 /app/data/churn_analysis.db
"""
import os
import shutil
import sqlite3
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

AUDIT_DIR = "/tmp/audit"
SNAPSHOT = os.path.join(AUDIT_DIR, "readonly_snapshot.db")
WRITABLE = os.path.join(AUDIT_DIR, "writable_copy.db")
PROD = "/app/data/churn_analysis.db"

_ro_engine = None
_rw_engine = None


def _check_snapshot():
    if not os.path.exists(SNAPSHOT):
        raise RuntimeError(
            f"快照不存在: {SNAPSHOT}\n"
            f"请先执行: mkdir -p {AUDIT_DIR} && cp {PROD} {SNAPSHOT}"
        )


def engine_readonly():
    """只读引擎 —— 用 URI mode=ro，任何写入都会被 SQLite 拒绝。"""
    global _ro_engine
    if _ro_engine is None:
        _check_snapshot()
        _ro_engine = create_engine(
            f"sqlite:///file:{SNAPSHOT}?mode=ro&uri=true",
            connect_args={"check_same_thread": False},
        )
    return _ro_engine


def session():
    """只读 Session（绑定到快照）。"""
    return sessionmaker(bind=engine_readonly())()


def session_writable():
    """可写 Session —— 但作用在**快照的副本**上，与生产库无关。

    每次调用都会从快照重新复制一份，保证实验之间互不污染。
    """
    global _rw_engine
    _check_snapshot()
    shutil.copyfile(SNAPSHOT, WRITABLE)
    _rw_engine = create_engine(f"sqlite:///{WRITABLE}",
                               connect_args={"check_same_thread": False})
    return sessionmaker(bind=_rw_engine)()


def guard_production(sess):
    """断言一个 session 没有连到生产库。"""
    url = str(sess.get_bind().url)
    if "churn_analysis.db" in url and "audit" not in url and "tmp" not in url:
        raise RuntimeError(f"⛔ 该 session 连着生产库，禁止用于实验: {url}")
    return True


def setup_from_prod():
    """把生产库复制成快照（只在明确需要刷新时调用）。"""
    os.makedirs(AUDIT_DIR, exist_ok=True)
    shutil.copyfile(PROD, SNAPSHOT)
    return SNAPSHOT


if __name__ == "__main__":
    setup_from_prod()
    print(f"快照已就绪: {SNAPSHOT}")
    db = session()
    from sqlalchemy import text
    n = db.execute(text("SELECT COUNT(*) FROM customers")).scalar()
    print(f"customers 行数 = {n}")
    # 验证只读
    try:
        db.execute(text("UPDATE customers SET cluster_id = 0 WHERE id = 1"))
        db.commit()
        print("⚠ 只读保护未生效！")
    except Exception as e:
        print(f"✅ 只读保护生效: {type(e).__name__}")
    db.close()
