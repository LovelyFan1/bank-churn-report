"""分块数据加载器 — 面向千万级数据规模。

核心原则:
- 永远不要一次性把全量数据加载到 pandas DataFrame
- 用 SQL 做聚合 / 过滤，用 chunksize 做流式迭代
- 训练时按 chunk 流式喂给模型 (partial_fit / MiniBatch)
"""

import pandas as pd
import numpy as np
import time
from typing import Iterator, Tuple, Optional, List
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.config import settings


# ── 特征定义 ──────────────────────────────────────────────

NUMERIC_FEATURES = [
    "credit_score", "age", "tenure", "balance", "num_products",
    "has_credit_card", "is_active_member", "estimated_salary",
    "satisfaction_score", "points_earned",
]

CATEGORICAL_FEATURES = ["geography", "gender"]

FEATURE_NAMES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

GEOGRAPHY_MAP = {"France": 0, "Germany": 1, "Spain": 2}
GENDER_MAP = {"Male": 0, "Female": 1}

# 聚类特征（包含 balance_salary_ratio）
CLUSTER_FEATURES = [
    "credit_score", "age", "tenure", "balance", "num_products",
    "has_credit_card", "is_active_member", "estimated_salary",
    "satisfaction_score", "points_earned", "balance_salary_ratio",
]


class DataLoader:
    """分块数据加载器 — 支持分块迭代和数据库内聚合。"""

    def __init__(self, db: Session, chunksize: int = None):
        self.db = db
        self.chunksize = chunksize or settings.DATA_CHUNK_SIZE

    @property
    def total_count(self) -> int:
        """客户总数（走 COUNT 查询，不加载数据）"""
        return self.db.query(Customer).count()

    # ── 分块迭代 ──────────────────────────────────────────

    def iter_chunks(self) -> Iterator[pd.DataFrame]:
        """逐块迭代全量客户数据，每块一个 DataFrame。

        用法:
            for chunk in loader.iter_chunks():
                X, y = prepare_features(chunk)
                model.partial_fit(X, y)
        """
        offset = 0
        while True:
            rows = (
                self.db.query(Customer)
                .order_by(Customer.id)
                .offset(offset)
                .limit(self.chunksize)
                .all()
            )
            if not rows:
                break
            yield self._rows_to_df(rows)
            offset += self.chunksize

    def load_chunk(self, offset: int, limit: int = None) -> pd.DataFrame:
        """加载指定偏移量的一块数据。"""
        limit = limit or self.chunksize
        rows = (
            self.db.query(Customer)
            .order_by(Customer.id)
            .offset(offset)
            .limit(limit)
            .all()
        )
        return self._rows_to_df(rows)

    def load_all(self) -> pd.DataFrame:
        """全量加载（仅适合万级数据；千万级请用 iter_chunks）。"""
        rows = self.db.query(Customer).all()
        return self._rows_to_df(rows)

    # ── 数据库内聚合（避免 Python 侧计算）─────────────────

    def count_by(self, column: str) -> dict:
        """数据库内 GROUP BY 计数，不加载原始行。"""
        from sqlalchemy import func
        results = (
            self.db.query(getattr(Customer, column), func.count(Customer.id))
            .group_by(getattr(Customer, column))
            .all()
        )
        return {str(r[0]): r[1] for r in results}

    def churn_rate_by(self, column: str) -> pd.DataFrame:
        """数据库内按列计算流失率。"""
        from sqlalchemy import func
        results = (
            self.db.query(
                getattr(Customer, column),
                func.count(Customer.id),
                func.sum(Customer.exited),
            )
            .group_by(getattr(Customer, column))
            .all()
        )
        df = pd.DataFrame(results, columns=["value", "total", "churned"])
        df["churn_rate"] = (df["churned"] / df["total"] * 100).round(2)
        return df

    # ── 内部工具 ──────────────────────────────────────────

    def _rows_to_df(self, rows) -> pd.DataFrame:
        """将 ORM 对象列表转为 DataFrame。"""
        return pd.DataFrame([{
            "id": r.id,
            "customer_id": r.customer_id,
            "surname": r.surname,
            "credit_score": r.credit_score,
            "geography": r.geography,
            "gender": r.gender,
            "age": r.age,
            "tenure": r.tenure,
            "balance": r.balance,
            "num_products": r.num_products,
            "has_credit_card": r.has_credit_card,
            "is_active_member": r.is_active_member,
            "estimated_salary": r.estimated_salary,
            "exited": r.exited,
            "complain": r.complain,
            "satisfaction_score": r.satisfaction_score,
            "points_earned": r.points_earned,
            "balance_salary_ratio": r.balance_salary_ratio,
            "cluster_id": r.cluster_id,
            "risk_score": r.risk_score,
            "risk_level": r.risk_level,
        } for r in rows])


# ── 特征工程（独立函数，方便 Celery 任务复用）─────────────

def prepare_features(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """从 DataFrame 提取特征矩阵 X、标签 y、特征名列表。"""
    y = df["exited"].values

    X_numeric = df[NUMERIC_FEATURES].values

    X_geography = df["geography"].map(GEOGRAPHY_MAP).fillna(0).values.reshape(-1, 1)
    X_gender = df["gender"].map(GENDER_MAP).fillna(0).values.reshape(-1, 1)

    X = np.hstack([X_numeric, X_geography, X_gender])

    return X, y, FEATURE_NAMES


def prepare_cluster_features(df: pd.DataFrame) -> np.ndarray:
    """提取聚类特征矩阵（已标准化以外的数值特征）。"""
    return df[CLUSTER_FEATURES].values


def get_customer_dataframe(db: Session) -> pd.DataFrame:
    """一次性全量加载（兼容旧接口，仅用于万级数据）。"""
    loader = DataLoader(db)
    return loader.load_all()


# ── 全量特征表：进程级共享缓存 ────────────────────────────
#
# 背景（实测）：10 万行下 DataLoader.load_all() 约 2.8 秒，而 EDA / 成本收益
# / 风险引擎三个服务各自调用一次，同一份数据被读了 4 遍。实测各接口耗时：
#     /api/eda/*               3.1 s
#     /api/cost-benefit/*      3.3 s
#     /api/portfolio/matrix    3.5 s
# 三处都只是「读同一张表」，没有各自重算的理由。
#
# ⚠ 与 risk_scoring._engine_cache 的关系（刻意解耦，不要合并）：
#   后者还缓存了**模型的原始概率**，模型重训后必须失效。
#   本缓存只代表「数据库里的客户表快照」，与模型无关，二者失效条件不同。
#   共用 risk_scoring 的缓存会让「重训模型」连带把 EDA 的缓存也清掉，
#   属于把一个模块的生命周期绑到另一个模块上。
#
# 失效方式：
#   - TTL 到期自动失效
#   - 写入客户数据后调用 invalidate_customer_cache() 显式失效
CUSTOMER_CACHE_TTL = 3600  # 秒。客户表变动频率低（播种/换数据源），1 小时足够

# 下游服务的列依赖，显式声明而非依赖巧合。
# 背景：DataLoader._rows_to_df() 目前不产出 row_number / card_type / age_group，
# 而 EDA 的接口按 feature 名放行，可能请求到 card_type。直接共享会让 EDA
# 从「读 DB 能拿到」变成「共享表没有 → KeyError」。这里补上，保持行为等价。
_REQUIRED_COLUMNS = {
    "row_number": lambda df: np.arange(1, len(df) + 1),
    "card_type": lambda df: None,   # 由调用方（读 DB）提供
    "age_group": lambda df: None,   # 由调用方提供；EDA 也会按需自行计算
}

_customer_cache = {"df": None, "ts": 0.0}


def invalidate_customer_cache() -> None:
    """客户表变更后调用（播种、换数据源、批量导入）。"""
    _customer_cache["df"] = None
    _customer_cache["ts"] = 0.0


def get_cached_customer_df(db: Session) -> pd.DataFrame:
    """全量客户 DataFrame（进程级缓存）。

    ⚠ 返回的是**共享对象**，调用方**不得原地修改**。
    需要加列/改列的，先 `df = df.copy()` 或改用局部变量 ——
    eda_service.get_age_distribution() 曾因此差点污染整张共享表。
    """
    if _customer_cache["df"] is not None and time.time() - _customer_cache["ts"] < CUSTOMER_CACHE_TTL:
        return _customer_cache["df"]

    loader = DataLoader(db)
    df = loader.load_all()

    # 补上下游依赖但 _rows_to_df 未产出的列。
    # row_number 需要与数据库的 id 对齐（DB 里 row_number 就是插入序号），
    # 但 _rows_to_df 不含该列 —— 这里从 Customer 表单独取，避免猜。
    missing = [c for c in _REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        from app.models.customer import Customer
        cols = [c for c in missing if hasattr(Customer, c)]
        if cols:
            rows = db.query(*[getattr(Customer, c) for c in cols]).order_by(Customer.id).all()
            for i, c in enumerate(cols):
                df[c] = [r[i] for r in rows]

    _customer_cache["df"] = df
    _customer_cache["ts"] = time.time()
    return df
