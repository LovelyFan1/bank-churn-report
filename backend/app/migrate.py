"""轻量 schema 迁移 —— 给**已存在**的表补新增列。

**为什么需要这个文件**

本项目没有 Alembic。`Base.metadata.create_all()` 只会创建**不存在的表**，
对已存在的表**不会加列** —— 这是 SQLAlchemy 的既定行为，不是 bug。

后果（实测）：给 `work_orders` 模型加 `created_by` 后，本地库里那张表
已经有数据，`create_all` 直接跳过，于是任何 `SELECT ... created_by`
都会抛 `sqlite3.OperationalError: no such column: work_orders.created_by`。
整个工单模块 500，而报错信息完全不提"缺迁移"，排查成本很高。

**为什么不用 Alembic**

Alembic 对单文件 SQLite + 演示项目是纯负担（多一套版本表与 migration
脚本目录），而本项目至今只加过列、没有改过类型或删过列
（"加列"是 SQLite 唯一能低成本增量完成的 DDL）。

**设计原则**

1. **幂等**：先查 PRAGMA 判断列是否存在，存在则跳过。每次启动都跑无害。
2. **只加列，不改类型、不删列** —— 后者在 SQLite 上需要重建表，
   风险与收益不成比例，真要做应该上 Alembic。
3. **失败不静默**：调用方（preseed）会把异常打出来并返回非 0 退出码。
   静默跳过会让人以为迁移成功，实际运行到某个接口才炸。

**调用时机**

必须早于**任何** ORM 查询。当前接在 `app/preseed.py` 的 `create_all()` 之后、
播种之前 —— 那条路径是 docker-compose 里 `python -m app.preseed && uvicorn`
的必经入口，保证先迁移再起服务。
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# 需要补的列：(表名, 列名, 列类型 DDL)
#
# ⚠ 每加一列就往这里追加一条；DDL 必须与 models 里的定义一致。
#   SQLite 的 ALTER TABLE ADD COLUMN 不支持加 UNIQUE / 主键，
#   只能加可空列或带常量默认值的列 —— 下面这些都是可空列，符合限制。
_PENDING_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # 建单人（行员号）。加列前建的工单为 NULL —— 这是事实，
    # 不编造填充值。前端显示为"建单人不明（历史数据）"。
    ("work_orders", "created_by", "VARCHAR"),
)


def _existing_columns(engine: Engine, table: str) -> set[str]:
    """读某张表当前的列名集合；表不存在时返回空集合。"""
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def run_migrations(engine: Engine) -> list[str]:
    """补齐缺失的列。返回本次实际执行的变更描述列表（供日志/测试断言）。

    幂等：重复调用在第二次起返回空列表。
    """
    applied: list[str] = []

    for table, column, ddl_type in _PENDING_COLUMNS:
        cols = _existing_columns(engine, table)
        if not cols:
            # 表还不存在 —— create_all 会连同新列一起建，无需 ALTER
            continue
        if column in cols:
            continue

        # 列名与类型都来自本模块的常量元组，不含用户输入，故直接拼接安全。
        # （不做参数化：DDL 的标识符本就不能用绑定参数。）
        stmt = f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl_type}'
        with engine.begin() as conn:
            conn.execute(text(stmt))
        applied.append(f"{table}.{column}")
        logger.info("migrate: 已为 %s 补列 %s", table, column)

    if applied:
        print(f"[migrate] 已补列：{', '.join(applied)}", flush=True)
    return applied
