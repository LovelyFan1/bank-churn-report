"""聚类服务 — 聚类结果读取 + 画像分析。

重算力任务（K-Means 拟合、肘部法则）已迁移到 Celery Worker:
    app/celery_tasks/cluster.py

本服务只负责:
- 读取已有聚类标签的数据，生成画像
- 3D 散点数据（PCA 降维）
- 聚类自动命名
"""

import json
import numpy as np
import pandas as pd
from pathlib import Path
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional

from app.models.customer import Customer
from app.config import settings
from app.services.data_loader import DataLoader, CLUSTER_FEATURES, prepare_cluster_features

from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA

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
        """加载全量数据（仅用于画像分析，聚类标签已落在 DB 中）。"""
        if self._df is None:
            loader = DataLoader(self.db)
            self._df = loader.load_all()
        return self._df

    # ── 聚类元数据（从磁盘读取上次聚类结果）─────────────────

    def get_cluster_meta(self) -> Optional[dict]:
        meta_path = CLUSTER_DIR / "cluster_meta.json"
        if not meta_path.exists():
            return None
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)

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

    def get_cluster_names(self) -> Dict[int, str]:
        """基于聚类特征自动命名。"""
        profiles = self.get_cluster_profiles()
        names = {}

        for cluster in profiles.get("clusters", []):
            cid = cluster["cluster_id"]
            f = cluster["features"]
            churn = cluster["churn_rate"]
            balance = f.get("balance", {}).get("mean", 0)
            products = f.get("num_products", {}).get("mean", 0)
            active = f.get("is_active_member", {}).get("mean", 0)
            salary = f.get("estimated_salary", {}).get("mean", 0)

            if churn > 40:
                names[cid] = "高流失风险客户"
            elif balance > 120000:
                names[cid] = "高余额价值客户"
            elif products > 2.5:
                names[cid] = "多产品忠诚客户"
            elif active < 0.2:
                names[cid] = "低活跃沉默客户"
            elif salary < 50000:
                names[cid] = "低薪价格敏感客户"
            elif active > 0.8 and churn < 15:
                names[cid] = "高活跃稳定客户"
            elif churn < 10:
                names[cid] = "低风险优质客户"
            else:
                names[cid] = "中等价值客户"

        return names

    # ── 3D 散点数据 ───────────────────────────────────────

    # 散点最多返回的点数。实测：全量返回 10 万点 → 响应体 **6.18 MB**、
    # 耗时 **10.9 秒**（是第二大的 /model/roc-curves 的 12 倍），而画布只有
    # 838×360 —— 10 万个点在物理上无法分辨，绝大多数像素重叠、纯属浪费。
    # 分层抽样到 1.5 万点后，分群结构在图上完全可见，体积降到约 1 MB。
    MAX_SCATTER_POINTS = 15000

    def get_3d_scatter_data(self) -> Dict[str, Any]:
        """获取 PCA 降维散点数据（用于前端可视化）。

        ⚠ 坐标轴裁剪的必要性（实测数据）：
          本图曾"看起来糊成一团"，根因是**未裁剪坐标轴**。PC2 的实测分布：
              全 range     = 92.72   （-90.9 ~ 1.8）
              1%~99% range =  3.09
              → 99% 的点只占全 range 的 **3.33%**
          也就是说 ECharts 按 -90.9~1.8 画轴时，10 万个客户被压成针尖大的一团。
          离群来源是 balance_salary_ratio（= balance/(salary+1)，当 salary 极小时
          该比值可达 8000+，是同列 p99 的 207 倍），它在 PC2 上载荷 -0.673。

          因此这里一并返回各主成分的 [0.5%, 99.5%] 分位区间供前端裁剪：
          实测裁剪只切掉 **1% 的点**，但视图立刻清晰。
          注意：裁剪只影响**显示范围**，不改变任何数据与聚类结果。
        """
        if not self.is_clustered:
            return {"data": [], "explained_variance": [], "error": "尚未执行聚类"}

        df = self._get_dataframe()
        clustered = df[df["cluster_id"].notna()].copy()

        # 提取特征并标准化 —— 走 prepare_cluster_features，
        # 它与聚类任务用的是**同一个**入口，内含离群截断
        # （balance_salary_ratio 最高 8319.98 = p99 的 207 倍，会把 PC2 轴
        #  拉伸到 92.7 宽度，而 99% 的点只占 3.09 → 不截断则散点图糊成一团）。
        # 保持与聚类同源，才能保证图上看到的分布就是聚类时用的分布。
        X = prepare_cluster_features(clustered)
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        # PCA 降维到 3 维
        pca = PCA(n_components=3)
        X_pca = pca.fit_transform(X_scaled)

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

        # ── 分层抽样：每个簇按相同比例取样，保证各簇在图上比例不失真 ──
        total_pts = len(clustered)
        if total_pts > self.MAX_SCATTER_POINTS:
            frac = self.MAX_SCATTER_POINTS / total_pts
            keep_idx = []
            for cid in sorted(clustered["cluster_id"].unique()):
                sub = clustered.index[clustered["cluster_id"] == cid]
                n_keep = max(1, int(round(len(sub) * frac)))
                # 固定随机种子：同一份数据每次抽样结果一致，便于对照与复现
                picked = np.random.default_rng(42).choice(sub, size=n_keep, replace=False)
                keep_idx.extend(picked.tolist())
            plotted = clustered.loc[sorted(keep_idx)]
            sampled = True
        else:
            plotted = clustered
            sampled = False

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
            "explained_variance": pca.explained_variance_ratio_.tolist(),
            # 前端据此裁剪坐标轴，避免离群值把主体压成一团
            "axis_range": axis_range,
            # 抽样说明：total > plotted 时前端需标注"已抽样展示"
            "total_points": int(total_pts),
            "plotted_points": int(len(plotted)),
            "sampled": sampled,
        }


def get_clustering_service(db: Session) -> ClusteringService:
    """工厂函数 — 每次创建新的 ClusteringService（无共享状态）。"""
    return ClusteringService(db)
