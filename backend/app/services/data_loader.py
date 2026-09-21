"""分块数据加载器 — 面向千万级数据规模。

核心原则:
- 永远不要一次性把全量数据加载到 pandas DataFrame
- 用 SQL 做聚合 / 过滤，用 chunksize 做流式迭代
- 训练时按 chunk 流式喂给模型 (partial_fit / MiniBatch)
"""

import os
import pandas as pd
import numpy as np
import time
from pathlib import Path
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

# ── 聚类特征的稳健缩放（去离群）──────────────────────────
#
# ⚠ 为什么必须做这件事（实测证据）：
#   `balance_salary_ratio = balance / (estimated_salary + 1)`，而**源 CSV 里
#   estimated_salary 最小值为 13.74**（年薪 14 元，属源数据质量问题），
#   使该比值最高达 **8319.98** —— 是同列 p99（40.15）的 **207 倍**。
#
#   后果有两层：
#     1) 画图：它在 PC2 上载荷 -0.673，一个离群值把 PC2 轴拉到 -90.9~1.8，
#        而 99% 的点只分布在 3.09 宽度的区间里 → 散点图糊成一团；
#     2) 聚类：StandardScaler 按方差缩放，这个维度被离群值主导，
#        直接拉低聚类质量（silhouette 仅 0.0718）。
#
#   实测该列分布：p99=40.15、p99.9=420.41、max=8319.98，
#   超过 10 的有 3805 行、超过 100 的有 433 行。
#
# 处理方式：按分位数截断（winsorize）到 [p0.5, p99.5]。
#   实测只影响 **0.5% 的行**（500/100000），但 max 从 8319.98 降到 84.69。
#   ⚠ 只在**聚类**路径上截断，不改数据库原始值、也不影响流失预测模型
#     （预测用的是 NUMERIC_FEATURES，本就不含该列）。
CLUSTER_WINSOR_QUANTILE = 0.005   # 双侧各截 0.5%


def winsorize_cluster_features(df: pd.DataFrame) -> pd.DataFrame:
    """对聚类特征做分位数截断，消除离群值对标准化与 PCA 的扭曲。

    返回**新的** DataFrame（不原地修改传入对象 —— 调用方可能持有共享缓存，
    见 get_cached_customer_df 的"不得原地修改"约定）。

    截断阈值按每列独立计算；对常量列（min==max）跳过，避免除零。
    """
    out = df[CLUSTER_FEATURES].copy()
    for col in CLUSTER_FEATURES:
        s = pd.to_numeric(out[col], errors="coerce")
        lo = s.quantile(CLUSTER_WINSOR_QUANTILE)
        hi = s.quantile(1 - CLUSTER_WINSOR_QUANTILE)
        if pd.notna(lo) and pd.notna(hi) and lo < hi:
            out[col] = s.clip(lo, hi)
        else:
            out[col] = s
    return out


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
    """提取聚类特征矩阵（含离群截断，见 winsorize_cluster_features 的说明）。

    ⚠ 这里做截断是**有意为之**：本函数是聚类（Celery 任务）与 PCA 散点
    （clustering_service）的公共入口，两处都需要去离群。此前直接
    `df[CLUSTER_FEATURES].values`，把 balance_salary_ratio 的 8319.98
    这类由源数据脏值（estimated_salary=13.74）产生的极端值原样喂给
    StandardScaler，导致 99% 的点在 PCA 图上被压成针尖一团。
    """
    return winsorize_cluster_features(df).values


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

_customer_cache = {"df": None, "ts": 0.0, "version": None}

# ── 跨进程缓存失效的版本标记 ──────────────────────────────
#
# ⚠ 为什么需要它（实测发现的严重缺陷）：
#   `invalidate_customer_cache()` 是**进程内**函数，只能清当前进程的内存缓存。
#   而本项目的重算力任务跑在 **Celery worker（独立进程）** 里：
#       Celery worker 写库 → 调 invalidate_customer_cache() → 只清 worker 自己
#       backend 的 4 个 uvicorn worker **完全不知道**，继续用旧快照。
#
#   实测（重新聚类 k=5 之后立刻请求接口）：
#       DB  cluster_id 分布 = {0:28382, 1:23748, 2:25539, 3:21080, 4:1251}   ← 新
#       GET /api/cluster/profiles 返回 count = 25577, 22760, 22649, 14343, 14671 ← 旧
#   两者不一致，且会持续到 CUSTOMER_CACHE_TTL（**整整 1 小时**）到期或
#   backend 重启为止。这正是「点了重新聚类，页面像没反应」的直接原因。
#
# 方案：把「缓存版本」落成一个极小的文件，写入方 bump 版本号，读取方每次
#   请求对比一次。`os.stat` + 读 1 个整数远比重新加载 10 万行（实测 2.8 秒）
#   便宜，因此可以每次调用都检查，不牺牲性能。
#
# 为什么用文件而不是 Redis：项目已依赖 Redis，但 data_loader 被 Celery 与
#   backend 共用，引入 Redis 客户端会增加一个部署耦合点（Redis 不可用时
#   连全量读都会失败）。文件方案零依赖、失败时静默降级为旧行为。
_CACHE_VERSION_FILE = Path(__file__).parent.parent.parent / "saved_models" / ".customer_cache_version"


def _read_cache_version() -> str:
    """读取当前缓存版本号；文件不存在或读失败时返回空串（降级为不感知）。"""
    try:
        return _CACHE_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def invalidate_customer_cache() -> None:
    """客户表变更后调用（播种、换数据源、批量导入、**聚类标签写回**）。

    ⚠ 本函数会**同时**：
      1) 清当前进程的内存缓存；
      2) 递增磁盘上的版本号 —— 让**其他进程**（Celery ↔ backend）也能感知。
    只做第 1 步是旧实现的缺陷，见上方版本标记的说明。
    """
    _customer_cache["df"] = None
    _customer_cache["ts"] = 0.0
    _customer_cache["version"] = None

    try:
        _CACHE_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        cur = _read_cache_version()
        try:
            nxt = int(cur) + 1 if cur else 1
        except ValueError:
            nxt = 1
        # 原子写：避免读方拿到写了一半的内容
        tmp = _CACHE_VERSION_FILE.with_name(_CACHE_VERSION_FILE.name + ".tmp")
        tmp.write_text(str(nxt), encoding="utf-8")
        os.replace(tmp, _CACHE_VERSION_FILE)
    except OSError:
        # 版本文件不可写不应让调用方失败（例如只读挂载）。
        # 此时退化为旧的"仅进程内失效"行为。
        pass


def get_cached_customer_df(db: Session) -> pd.DataFrame:
    """全量客户 DataFrame（进程级缓存 + 跨进程版本校验）。

    ⚠ 返回的是**共享对象**，调用方**不得原地修改**。
    需要加列/改列的，先 `df = df.copy()` 或改用局部变量 ——
    eda_service.get_age_distribution() 曾因此差点污染整张共享表。
    """
    cur_version = _read_cache_version()
    fresh = (
        _customer_cache["df"] is not None
        and time.time() - _customer_cache["ts"] < CUSTOMER_CACHE_TTL
        # 版本一致才复用：其他进程（Celery）写库后会 bump 版本号
        and _customer_cache["version"] == cur_version
    )
    if fresh:
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
    _customer_cache["version"] = cur_version
    return df
