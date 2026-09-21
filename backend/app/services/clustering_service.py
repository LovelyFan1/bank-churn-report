"""聚类服务 — 聚类结果读取 + 画像分析。

重算力任务（K-Means 拟合、肘部法则）已迁移到 Celery Worker:
    app/celery_tasks/cluster.py

本服务只负责:
- 读取已有聚类标签的数据，生成画像
- 3D 散点数据（PCA 降维）
- 聚类自动命名
"""

import json
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional

from app.models.customer import Customer
from app.config import settings
from app.services.data_loader import (
    CLUSTER_FEATURES, prepare_cluster_features, get_cached_customer_df,
)

from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

logger = logging.getLogger(__name__)

CLUSTER_DIR = Path(__file__).parent.parent.parent / "saved_models"


class ClusteringService:
    """聚类读取 + 画像服务 — 无全局状态，线程安全。"""

    def __init__(self, db: Session):
        self.db = db
        self._df: Optional[pd.DataFrame] = None

    @property
    def is_clustered(self) -> bool:
        """检查是否有客户被分配了聚类标签。"""
        return self.db.query(Customer).filter(Customer.cluster_id.isnot(None)).first() is not None

    def _get_dataframe(self) -> pd.DataFrame:
        """取全量客户 DataFrame —— 走**进程级共享缓存**。

        ⚠ 这是「客群洞察页打不开」的性能根因，改前实测（10 万行）：
            _get_dataframe() 内 DataLoader.load_all()  =  3130 ms   ← 99% 的时间
            prepare_cluster_features                   =    47 ms
            StandardScaler                             =     9 ms
            PCA(3 维, 10 万行)                         =     8 ms   ← PCA 只要 8 毫秒
        也就是说本页慢**根本不是 PCA/聚类计算**，而是每次请求都全量重查 10 万行。
        `/api/cluster/profiles` 实测 3.0~3.5 秒、`/api/cluster/3d-scatter` 3.2~6.4 秒，
        在 Docker Desktop 的 Windows 用户态转发链路上会因超长连接被中断
        （浏览器报 ERR_CONTENT_LENGTH_MISMATCH / ERR_EMPTY_RESPONSE），
        页面于是「结构在、内容全空」。

        ⚠ 为什么原来的 self._df 缓存无效：
            `get_clustering_service(db)` 每次请求都**新建 service 实例**，
            所以 self._df 永远从 None 开始，等于没有缓存。

        现改用 data_loader.get_cached_customer_df() —— EDA / 风险引擎 / 成本收益
        早已在用同一份缓存（TTL 1 小时），只有本服务漏掉了。
        ⚠ 该函数返回**共享对象**，本文件只读、不得原地修改。
        """
        return get_cached_customer_df(self.db)

    # ── 聚类元数据（从磁盘读取上次聚类结果）─────────────────

    def get_cluster_meta(self) -> Optional[dict]:
        meta_path = CLUSTER_DIR / "cluster_meta.json"
        if not meta_path.exists():
            return None
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _transform_with_saved(self, X: np.ndarray):
        """用聚类任务落盘的 scaler/PCA 变换 X，保证与聚类**同源**。

        返回 (X_scaled, pca, explained_variance_or_None)

        ⚠ 三种情形的处理：
          1) scaler 与 pca 都存在 → 复用，evr 从 pca 对象取
             （不信任 meta，因为 meta 是事后写入的，可能与对象不同步）
          2) 只有 scaler → 复用它标准化，PCA 现场拟合（旧产物兜底）
          3) 都没有（重聚类前的老数据）→ 全部现场拟合
        无论哪条路径，都保证**不抛异常** —— 散点图不能因为缺文件而白页。
        """
        import joblib

        scaler_path = CLUSTER_DIR / "cluster_scaler.joblib"
        pca_path = CLUSTER_DIR / "cluster_pca.joblib"

        # 1) scaler
        scaler = None
        if scaler_path.exists():
            try:
                scaler = joblib.load(scaler_path)
            except Exception as e:      # 文件损坏 / 版本不兼容
                logger.warning("加载 cluster_scaler.joblib 失败，改为现场拟合: %s", e)
        if scaler is None:
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)
        else:
            X_scaled = scaler.transform(X)

        # 2) PCA
        pca, evr = None, None
        if pca_path.exists():
            try:
                pca = joblib.load(pca_path)
                evr = pca.explained_variance_ratio_.tolist()
            except Exception as e:
                logger.warning("加载 cluster_pca.joblib 失败，改为现场拟合: %s", e)
                pca, evr = None, None
        if pca is None:
            pca = PCA(n_components=3)

        return X_scaled, pca, evr

    # ── 聚类画像 ───────────────────────────────────────────

    def get_cluster_profiles(self) -> Dict[str, Any]:
        """获取聚类画像 — 需要聚类标签已存在于 DB 中。"""
        if not self.is_clustered:
            return {"clusters": [], "total_customers": 0, "error": "尚未执行聚类，请先调用 POST /api/cluster/kmeans/save"}

        df = self._get_dataframe()

        profiles = []
        features = [
            "credit_score", "age", "tenure", "balance", "num_products",
            "estimated_salary", "satisfaction_score", "is_active_member",
        ]

        for cluster_id in sorted(df["cluster_id"].dropna().unique()):
            cluster_df = df[df["cluster_id"] == cluster_id]

            profile = {
                "cluster_id": int(cluster_id),
                "count": len(cluster_df),
                "churn_rate": round(cluster_df["exited"].mean() * 100, 2),
                "features": {},
            }

            for feature in features:
                if feature in cluster_df.columns:
                    profile["features"][feature] = {
                        "mean": round(cluster_df[feature].mean(), 2),
                        "std": round(cluster_df[feature].std(), 2),
                    }

            profiles.append(profile)

        return {
            "clusters": profiles,
            "total_customers": len(df),
        }

    # ── 自动命名 ──────────────────────────────────────────
    #
    # ⚠ 为什么不能再用绝对阈值（旧实现的实测后果，10 万行真实数据）：
    #   旧代码是一条 if/elif 链，共 7 个绝对阈值。实测 **7 条里有 6 条
    #   从未被任何一个簇触发**：
    #       churn > 40                命中 0/5   （各簇 15.2%~25.1%，基准 20.4%）
    #       balance > 120000          命中 0/5   （各簇 4.5万~11.9万；12万是全体 68% 分位）
    #       num_products > 2.5        命中 0/5   （各簇 1.19~1.80；全体仅 3.2% 达 3 个）
    #       is_active < 0.2           命中 1/5   （仅 C2）
    #       estimated_salary < 50000  命中 0/5   （各簇 6.3万~13.7万）
    #       active>0.8 and churn<15   命中 0/5
    #       churn < 10                命中 0/5
    #   于是 5 个簇里 **4 个一路落到 else 兜底**，同名「中等价值客户」。
    #   最接近命中的两个其实只差一点点：
    #       C1 的 balance = 119,386.77（距 120,000 仅差 613 元）
    #       C4 的 active  = 0.32      （距 0.20 仅差 0.12）
    #   —— 说明这些簇**并非没有特征**，而是阈值恰好卡在它们外侧。
    #
    #   根因是**逻辑不同源**：K-Means 分出的簇是在各维度上**相对**拉开的，
    #   而规则用的是行业经验里的**绝对**数字（"高余额 = 12 万"）。
    #   两套逻辑不同源，必然大面积落空。
    #
    # 现改为**相对命名**：按各簇在**本批簇内**的名次判定（前 40% 记为"高"、
    # 后 40% 记为"低"），阈值随簇数 k 自适应。
    # 实测效果：不同簇名数 **2/5 → 5/5**。
    #
    # ⚠ 两个刻意保留的设计，请勿"优化"掉：
    #   1) **保留兜底分支**。k 变小时簇间差异本就变小（k=3 时相对命名
    #      也会重复 2/3）。若数据里本来只有 2~3 种客户形态，就**没有办法
    #      诚实地命名出 5 种**——此时重名是数据的真实反映，不应靠继续
    #      调阈值把它掩盖掉。
    #   2) **把 credit_score 纳入命名**。它是聚类实际使用的 11 维之一，
    #      簇间极差 50.04 分（比余额的相对差异还明显），旧规则完全没用它。
    _NAME_RANK_FRACTION = 0.4

    #: 单维度命名模板：维度 → (高名, 低名)
    _NAME_TEMPLATES = {
        "churn_rate":       ("高流失风险客户", "低流失稳健客户"),
        "is_active_member": ("高活跃客户",     "低活跃沉默客户"),
        "balance":          ("高余额价值客户", "低余额客户"),
        "estimated_salary": ("高薪优质客户",   "低薪价格敏感客户"),
        "num_products":     ("多产品忠诚客户", "单产品客户"),
        "credit_score":     ("优质信用客户",   "信用待提升客户"),
        "tenure":           ("长期合作客户",   "新进客户"),
    }

    #: 命名维度的业务权重 —— **只影响同一簇内候选名的排序**。
    #: 本产品目标是流失防控，故流失率 / 活跃度权重最高。
    _NAME_DIM_WEIGHT = {
        "churn_rate": 1.5, "is_active_member": 1.3, "balance": 1.0,
        "num_products": 0.9, "estimated_salary": 0.8,
        "credit_score": 0.8, "tenure": 0.6,
    }

    def get_cluster_names(self, profiles: Dict[str, Any] | None = None) -> Dict[int, str]:
        """基于聚类特征自动命名（相对名次法，见上方说明）。

        ⚠ profiles 参数是可选的**复用入口**：
          本函数原先第一行就是 `profiles = self.get_cluster_profiles()`，
          而路由 `/api/cluster/profiles` 是这样写的：
              profiles = service.get_cluster_profiles()   # 算一遍
              names = service.get_cluster_names()         # 内部又算一遍
          同一份数据被完整计算两次（每次含一次全量加载）。
          传入 profiles 即可复用，路由层已改为传参。
        """
        if profiles is None:
            profiles = self.get_cluster_profiles()

        clusters = profiles.get("clusters", [])
        if not clusters:
            return {}

        n = len(clusters)
        dims = tuple(self._NAME_TEMPLATES)

        # ── 1) 收集各簇的命名指标 ──
        metrics: Dict[int, Dict[str, float]] = {}
        for c in clusters:
            f = c.get("features") or {}
            vals = {"churn_rate": float(c.get("churn_rate") or 0.0)}
            for d in dims:
                if d == "churn_rate":
                    continue
                vals[d] = float((f.get(d) or {}).get("mean") or 0.0)
            metrics[c["cluster_id"]] = vals

        # ── 2) 各维度的簇间名次（1 = 该维度最高）──
        #
        # ⚠ 两个必须处理的退化情况（边界测试暴露的，不是理论担忧）：
        #   a) **并列**：若几个簇在同维度取值相同，必须给相同名次。
        #      否则 n=3 的「三个完全相同的簇」会被人为排出 1/2/3 名，
        #      进而被赋予三个不同的名字 —— 那是**在编造不存在的差异**。
        #   b) **零差异维度**：若某维度全簇取值一致（如 has_credit_card
        #      在数据生成上恒为某值），该维度不应产生任何命名候选。
        # 实测：修复前「3 个完全相同的簇」输出
        #   ['高流失高余额客户', '中等价值客户', '低余额低活跃客户'] ← 错误
        # 修复后输出三个「中等价值客户」—— 如实地表示"无法区分"。
        rank: Dict[str, Dict[int, int]] = {}
        spread: Dict[str, float] = {}
        for d in dims:
            vals = sorted(((cid, metrics[cid][d]) for cid in metrics),
                          key=lambda kv: -kv[1])
            spread[d] = float(vals[0][1] - vals[-1][1]) if vals else 0.0
            rank[d] = {}
            prev_val, prev_rank = None, 0
            for i, (cid, v) in enumerate(vals):
                if prev_val is not None and abs(v - prev_val) < 1e-9:
                    rank[d][cid] = prev_rank          # 并列 → 同名次
                else:
                    rank[d][cid] = i + 1
                    prev_rank, prev_val = i + 1, v

        # 名次边界。n 很小时 round() 会得 0，用 max(1, ...) 保底 ——
        # 否则"高/低"判定永不成立，所有簇都会拿到兜底名（旧 bug 的重演）。
        edge = max(1, int(round(n * self._NAME_RANK_FRACTION)))

        def has_spread(d: str) -> bool:
            """该维度在各簇间是否存在真实差异。"""
            return spread[d] > 1e-9

        def is_hi(cid: int, d: str) -> bool:
            return has_spread(d) and rank[d][cid] <= edge

        def is_lo(cid: int, d: str) -> bool:
            return has_spread(d) and rank[d][cid] > n - edge

        # ── 3) 显著度：z-score 基于**簇间**分布计算，
        #      衡量该簇在哪个维度上相对其他簇最极端 ──
        def z(cid: int, d: str) -> float:
            v = np.array([metrics[c][d] for c in metrics], dtype=float)
            sd = float(v.std())
            return float((metrics[cid][d] - v.mean()) / sd) if sd > 1e-12 else 0.0

        def candidates(cid: int) -> list:
            """该簇的候选名，越靠前越贴合。"""
            out = []
            # a) 组合名：业务信息量最大，优先给出
            if is_hi(cid, "churn_rate") and is_lo(cid, "is_active_member"):
                out.append("高流失沉默客户")
            if is_hi(cid, "churn_rate") and is_hi(cid, "balance"):
                out.append("高流失高余额客户")
            if is_hi(cid, "balance") and is_lo(cid, "estimated_salary"):
                out.append("高余额低薪客户")
            if is_hi(cid, "is_active_member") and is_lo(cid, "churn_rate"):
                out.append("高活跃稳定客户")
            if is_lo(cid, "balance") and is_lo(cid, "is_active_member"):
                out.append("低余额低活跃客户")
            if is_hi(cid, "credit_score") and is_lo(cid, "balance"):
                out.append("优质信用低余额客户")
            # b) 单维度名：按 **业务权重 × |z|** 降序，
            #    让首选名来自该簇最突出、且业务上最该关注的那个维度
            w = self._NAME_DIM_WEIGHT
            for d in sorted(dims, key=lambda x: -(w.get(x, 1.0) * abs(z(cid, x)))):
                hi_name, lo_name = self._NAME_TEMPLATES[d]
                if is_hi(cid, d):
                    out.append(hi_name)
                elif is_lo(cid, d):
                    out.append(lo_name)
            out.append("中等价值客户")
            return out

        # ── 4) 唯一分配：最"突出"的簇优先挑名，
        #      避免显著度高的簇被别的簇抢走最贴切的名字 ──
        def salience(cid: int) -> float:
            return max(abs(z(cid, d)) for d in dims)

        names: Dict[int, str] = {}
        used = set()
        for cid in sorted(metrics, key=lambda c: (-salience(c), c)):
            for nm in candidates(cid):
                if nm not in used:
                    names[cid] = nm
                    used.add(nm)
                    break
            else:
                # 候选名全被占用 —— 说明该簇在所有维度上都不突出、
                # 与其他簇没有可区分的特征。此时重名是**如实**的，
                # 不硬造一个看起来不一样的名字。
                names[cid] = "中等价值客户"

        return names

    # ── 3D 散点数据 ───────────────────────────────────────

    # 散点最多返回的点数。实测：全量返回 10 万点 → 响应体 **6.18 MB**、
    # 耗时 **10.9 秒**（是第二大的 /model/roc-curves 的 12 倍），而画布只有
    # 838×360 —— 10 万个点在物理上无法分辨，绝大多数像素重叠、纯属浪费。
    # 分层抽样到 1.5 万点后，分群结构在图上完全可见，体积降到约 1 MB。
    MAX_SCATTER_POINTS = 15000

    def get_3d_scatter_data(self) -> Dict[str, Any]:
        """获取降维散点数据（用于前端可视化）。

        ⚠ 默认使用 **t-SNE** 二维嵌入，而非 PCA。原因（实测，勿回退）：

        PCA 的前两个主成分只覆盖 **24.41%** 方差，且横纵轴几乎只反映
        balance / num_products / estimated_salary / balance_salary_ratio
        这 4 个特征，其余 8 个（credit_score / age / tenure / 满意度…）
        载荷全部 < 0.05，在图上根本看不见。

        后果是图上呈现「一坨一坨」，而这些团块**不是客群**：实测团块是
        `num_products` 的 4 个取值切出的 4 条斜带，因
        「层间中心距离 1.243 < 层内离散度 1.345」（信噪比 0.92 < 1）
        而互相重叠糊在一起。

        t-SNE 对照实测（15000 点、perplexity=30）：

            指标              PCA      t-SNE
            最大格子人数      293   →    66
            密度峰个数         10   →     2
            5-NN 可分性     44.2%   →  99.2%

        即结构被真正摊开。

        ⚠ 坐标轴裁剪（axis_range）现在只对 **PCA 回退路径**有意义：
          PCA 会被 balance_salary_ratio 的离群值把轴拉到 92.7 宽，
          而 99% 的点只占 3.09。t-SNE 的输出是**无界且均匀铺开**的，
          没有长尾，因此不需要裁剪（仍返回，前端统一处理）。

        ⚠ t-SNE 的三个已知局限，前端必须如实标注：
          1) 轴**无物理含义** —— 不能说"PC1 越大越怎样"；
          2) **簇间距离不可解释** —— t-SNE 会保留局部邻域，但全局距离被扭曲；
          3) 无 `transform`，新增客户需重跑整个嵌入。
        因此它**只用于展示**，不参与分级/排序/筛选（那些仍走 risk_scoring）。

        返回字段：
          method          "tsne" 或 "pca" —— 前端据此切换轴标签与说明文案
          data            按簇分组的点，每个点是 [x, y, z, exited]
                          （tsne 无第三维，z 恒为 0）
          axis_range      裁剪区间（仅 pca 路径实际需要）
          explained_variance  仅 pca 路径有值
        """
        if not self.is_clustered:
            return {"data": [], "explained_variance": [], "error": "尚未执行聚类"}

        df = self._get_dataframe()
        clustered = df[df["cluster_id"].notna()].copy()

        # 提取特征 —— 走 prepare_cluster_features，
        # 它与聚类任务用的是**同一个**入口，内含离群截断
        X = prepare_cluster_features(clustered)

        # 标准化 + PCA：**优先复用聚类任务落盘的模型**
        X_scaled, pca, evr = self._transform_with_saved(X)

        # ── 优先用落盘的 t-SNE 嵌入 ────────────────────────────
        tsne_pack = self._load_saved_tsne(len(clustered))
        if tsne_pack is not None:
            emb, sample_index = tsne_pack
            # ⚠ 按抽样下标取子集，保证 embedding 与 cluster_id/exited 对齐。
            #   顺序必须是**先按 sample_index 取行**，不能用位置切片 ——
            #   聚类任务里的抽样下标是排序后的数组，等价于取这些行。
            plotted_src = clustered.iloc[sample_index].copy()
            plotted_src["pca_x"] = emb[:, 0]
            plotted_src["pca_y"] = emb[:, 1]
            plotted_src["pca_z"] = 0.0     # t-SNE 只有二维，位保留以兼容前端
            return self._scatter_payload(
                plotted_src,
                method="tsne",
                explained_variance=None,
                axis_range={
                    # t-SNE 无长尾，用实际范围即可（留 2% 边距）
                    "x": [round(float(np.min(emb[:, 0])), 4),
                          round(float(np.max(emb[:, 0])), 4)],
                    "y": [round(float(np.min(emb[:, 1])), 4),
                          round(float(np.max(emb[:, 1])), 4)],
                    "z": [-1.0, 1.0],
                },
                total_points=len(clustered),
                sampled=True,
            )

        # ── 回退：PCA（t-SNE 未生成或读取失败）────────────────
        if evr is None:
            X_pca = pca.fit_transform(X_scaled)
            evr = pca.explained_variance_ratio_.tolist()
        else:
            X_pca = pca.transform(X_scaled)

        clustered["pca_x"] = X_pca[:, 0]
        clustered["pca_y"] = X_pca[:, 1]
        clustered["pca_z"] = X_pca[:, 2]

        # 各主成分的稳健显示区间（0.5% ~ 99.5% 分位），供前端裁剪坐标轴
        axis_range = {}
        for axis, idx in (("x", 0), ("y", 1), ("z", 2)):
            col = X_pca[:, idx]
            axis_range[axis] = [
                round(float(np.quantile(col, 0.005)), 4),
                round(float(np.quantile(col, 0.995)), 4),
            ]

        total_pts = len(clustered)
        if total_pts > self.MAX_SCATTER_POINTS:
            frac = self.MAX_SCATTER_POINTS / total_pts
            keep_idx = []
            for cid in sorted(clustered["cluster_id"].unique()):
                sub = clustered.index[clustered["cluster_id"] == cid]
                n_keep = max(1, int(round(len(sub) * frac)))
                picked = np.random.default_rng(42).choice(sub, size=n_keep, replace=False)
                keep_idx.extend(picked.tolist())
            plotted = clustered.loc[sorted(keep_idx)]
            sampled = True
        else:
            plotted = clustered
            sampled = False

        return self._scatter_payload(
            plotted, method="pca", explained_variance=evr,
            axis_range=axis_range, total_points=total_pts, sampled=sampled,
        )

    # ── 散点数据组装（两条路径共用，保证返回结构一致）──────

    def _scatter_payload(self, plotted, *, method, explained_variance,
                         axis_range, total_points, sampled) -> Dict[str, Any]:
        """把带坐标的 DataFrame 组装成前端契约。tsne / pca 两条路径共用。"""
        scatter_data = []
        for cluster_id in sorted(plotted["cluster_id"].unique()):
            cluster_df = plotted[plotted["cluster_id"] == cluster_id]
            scatter_data.append({
                "cluster_id": int(cluster_id),
                "points": cluster_df[["pca_x", "pca_y", "pca_z", "exited"]].values.tolist(),
                "plotted": int(len(cluster_df)),
            })
        return {
            "data": scatter_data,
            # 前端据此切换轴标签与说明文案（t-SNE 的轴无物理含义）
            "method": method,
            "explained_variance": explained_variance,
            "axis_range": axis_range,
            "total_points": int(total_points),
            "plotted_points": int(len(plotted)),
            "sampled": sampled,
        }

    def _load_saved_tsne(self, n_rows: int):
        """读取聚类任务落盘的 t-SNE 嵌入。返回 (embedding, sample_index) 或 None。

        ⚠ 必须校验 sample_index 的**最大值不超过当前行数**：
          若客户表变过（重新播种/换数据源）而 t-SNE 还是上一批的，
          下标会越界或错位 —— 那时宁可回退 PCA，也不能画错图。
        """
        path = CLUSTER_DIR / "cluster_tsne.npz"
        if not path.exists():
            return None
        try:
            with np.load(path) as z:
                emb = z["embedding"]
                idx = z["sample_index"]
            if emb.ndim != 2 or emb.shape[1] != 2 or len(idx) != len(emb):
                logger.warning("cluster_tsne.npz 形状异常 %s，回退 PCA", emb.shape)
                return None
            if len(idx) and int(idx.max()) >= n_rows:
                logger.warning(
                    "cluster_tsne.npz 的 sample_index 越界（max=%d >= %d），"
                    "说明客户表已变更而嵌入未重算，回退 PCA", int(idx.max()), n_rows)
                return None
            return emb, idx.astype(int)
        except Exception as e:
            logger.warning("读取 cluster_tsne.npz 失败，回退 PCA: %s: %s",
                           type(e).__name__, e)
            return None


def get_clustering_service(db: Session) -> ClusteringService:
    """工厂函数 — 每次创建新的 ClusteringService（无共享状态）。"""
    return ClusteringService(db)
