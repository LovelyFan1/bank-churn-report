"""数据库连接 —— SQLite 并发口径（E04）。

**为什么默认不用 WAL**

当前部署是 `WEB_WORKERS=4` + Celery 进程，共用同一个 SQLite 文件。
WAL 能改善读写并发，但仍只有一个并发写者，也不能保证永不 BUSY；
而且 SQLite 官方记录过旧版本 WAL-reset 缺陷，3.51.3 及后续版本才修复。

本次核实：容器内 Python 实际加载 **SQLite 3.46.1**（Debian 13 的
libsqlite3-0 3.46.1），低于该修复版本。**没有证据表明该发行版已回补**，
因此不切换 journal_mode —— 用不确定的运行时去换取"看起来更快"，
代价是数据库损坏风险，对本系统不可接受。

故本文件只做两件**确定安全**的事：

1. 每个连接统一设置 `busy_timeout`（锁等待是**连接级**行为，
   只在一次初始化连接上设置不会对其余连接生效）；
2. 提供一个只读探测函数 `sqlite_runtime_info()`，供启动日志、诊断脚本
   与回归测试报告实际运行库版本、journal 模式与等待上限。

WAL 的启用方案与回退步骤见 `docs/SQLITE_CONCURRENCY_2026-10-23.md`：
在隔离环境验证过运行库版本、初始化幂等与恢复演练之前，不切换正式库。
"""

from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

# 锁等待上限（毫秒）。与 connect_args 的 timeout=30 保持一致 ——
# 两处口径必须相同，否则"到底等多久"取决于哪一层先触发。
BUSY_TIMEOUT_MS = 30000

_IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False,
                  "timeout": BUSY_TIMEOUT_MS // 1000} if _IS_SQLITE else {},
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


if _IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - 触发于连接建立
        """每个新连接都设置等待上限。

        ⚠ 这里**刻意不切换 journal_mode**：切换必须由受控单进程在业务
          worker 之前完成，且不能在每个连接上争抢（见模块说明）。
        """
        cur = dbapi_connection.cursor()
        try:
            cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        finally:
            cur.close()


def sqlite_runtime_info() -> dict:
    """只读探测：实际运行库版本、journal 模式、等待上限。

    用于启动日志、诊断与 E04 回归断言。**不修改任何 PRAGMA。**
    """
    info = {"database_url": settings.DATABASE_URL, "is_sqlite": _IS_SQLITE}
    if not _IS_SQLITE:
        return info
    with engine.connect() as conn:
        info["sqlite_version"] = conn.execute(text("select sqlite_version()")).scalar()
        info["journal_mode"] = conn.execute(text("PRAGMA journal_mode")).scalar()
        info["busy_timeout_ms"] = conn.execute(text("PRAGMA busy_timeout")).scalar()
        info["foreign_keys"] = conn.execute(text("PRAGMA foreign_keys")).scalar()
    return info


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
