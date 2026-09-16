"""数据源播种 —— 决定系统启动时往 customers 表灌哪份数据。

两种来源，由 `settings.DATA_SOURCE` 切换：

    generator  用 data_generator 现算合成数据（默认）
               优点：无条件可跑，不依赖外部文件。旧数据就是这条路径。
               代价：数据是造的，模型学的是造它的规则。

    csv        从 `settings.DATA_CSV_PATH` 读一份外部 CSV（标定数据）
               优点：统计结构标定自公开真实数据集，可复现、可对照。
               代价：需要外部文件；文件缺失时**明确失败**而不是静默回退
                     —— 静默回退会让人以为在用真实数据，实际在用合成数据。

⚠ 切换数据源必须清空 customers 表，原因见 config.DATA_SOURCE 的说明：
   两套数据的 customer_id 完全重叠（都是 C000001~C0xxxxx）但指向不同的人。
   叠加会得到两套记录共用一个编号的脏数据，且不会报任何错。
   本模块的 `seed()` 会检查并拒绝在「已有不同来源数据」时静默追加。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from sqlalchemy.orm import Session

from app.config import settings
from app.models.customer import Customer
from app.models.work_order import WorkOrder

# CSV 里可能不存在的列 —— 这些是 Customer 模型要求但外部数据未必提供的
_DERIVED_COLUMNS = ["age_group", "balance_salary_ratio"]


def _model_columns() -> list[str]:
    return [c.name for c in Customer.__table__.columns]


def load_dataframe() -> tuple[pd.DataFrame, str]:
    """按配置返回 (df, 来源说明)。失败时抛异常，不做静默回退。"""
    src = (settings.DATA_SOURCE or "generator").strip().lower()

    if src == "csv":
        path = Path(settings.DATA_CSV_PATH or "")
        if not path or not path.exists():
            raise FileNotFoundError(
                f"DATA_SOURCE=csv 但 DATA_CSV_PATH 指向的文件不存在: {path!r}。"
                f"请检查 docker-compose 的挂载与路径。"
            )
        df = pd.read_csv(path)
        note = f"外部 CSV: {path.name}"

        # 只保留模型认识的列。多出的列（时间/行为/产品明细等）当前流水线
        # 用不上，直接忽略；缺失的列填空值。
        # `id` 由数据库自增，不算"缺失列"，须在比对前排除 —— 否则日志里
        # 会把它当成缺列报出来（曾是本模块的一个小瑕疵）。
        known = [c for c in _model_columns() if c != "id"]
        extra = [c for c in df.columns if c not in known]
        missing = [c for c in known if c not in df.columns]
        df = df[[c for c in df.columns if c in known]].copy()
        if extra:
            note += f"；忽略 {len(extra)} 个模型不含的列"
        if missing:
            note += f"；补齐 {len(missing)} 个缺失列（{missing}）"
            for c in missing:
                df[c] = None

    elif src == "generator":
        from app.services.data_generator import get_data_generator
        df = get_data_generator().generate()
        note = f"生成器 (seed={settings.RANDOM_STATE}, n={settings.NUM_CUSTOMERS})"

    else:
        raise ValueError(
            f"未知的 DATA_SOURCE={settings.DATA_SOURCE!r}，只支持 'generator' / 'csv'"
        )

    # 派生字段：CSV 若已带则不重算，保证与文件内容一致
    if "age_group" in df.columns and df["age_group"].isna().any():
        df["age_group"] = pd.cut(
            df["age"], bins=[0, 25, 35, 45, 55, 100],
            labels=["18-25", "26-35", "36-45", "46-55", "56+"],
        ).astype(str)
    if "balance_salary_ratio" in df.columns and df["balance_salary_ratio"].isna().any():
        df["balance_salary_ratio"] = (df["balance"] / (df["estimated_salary"] + 1)).round(4)

    # 这两个字段由风险引擎实时计算并缓存，不落库；置空避免 CSV 里的值
    # 与引擎算出来的不一致（customer 表里它们全是 NULL）
    for c in ("cluster_id", "risk_score", "risk_level"):
        if c in df.columns:
            df[c] = None

    return df, note


def seed(db: Session, force: bool = False) -> dict:
    """往 customers 表灌数据。返回统计信息。

    只有在表为空时才灌（除非 force=True）。若表非空且数据源已变，
    会**明确报警并拒绝**，而不是静默追加 —— 见模块 docstring。
    """
    existing = db.query(Customer).count()
    if existing and not force:
        return {"seeded": False, "existing": existing, "reason": "已有数据，跳过"}

    df, note = load_dataframe()

    # 悬空工单检查：客户被换掉后，旧工单会指向不相干的人
    orders = db.query(WorkOrder).count()
    if orders:
        print(
            f"[seed] ⚠ 检测到 {orders} 条工单。换数据源后这些工单的 "
            f"customer_id 会指向**另一批人**（两套数据编号完全重叠但人不同），"
            f"建议一并清理，否则工单详情会张冠李戴。",
            file=sys.stderr,
        )

    records = df.to_dict(orient="records")
    # 分批写入：10 万行一次性 bulk_save_objects 在 SQLite 上峰值内存较高
    BATCH = 20000
    for i in range(0, len(records), BATCH):
        db.bulk_save_objects([Customer(**r) for r in records[i:i + BATCH]])
        db.commit()

    # 客户表已变，必须让全量特征表的进程级缓存失效 ——
    # 否则 EDA / 成本收益 / 风险引擎会继续用旧数据（换数据源时尤其危险：
    # 新旧编号重叠但人不同，界面会显示一批「不存在的人」）。
    from app.services.data_loader import invalidate_customer_cache
    invalidate_customer_cache()

    total = db.query(Customer).count()
    churn = db.query(Customer).filter(Customer.exited == 1).count()
    return {
        "seeded": True,
        "source": note,
        "rows": len(df),
        "total": total,
        "churn_rate": round(churn / total * 100, 2) if total else 0.0,
    }
