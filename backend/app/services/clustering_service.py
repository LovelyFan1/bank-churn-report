import pandas as pd
import numpy as np
from sqlalchemy.orm import Session
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from sklearn.decomposition import PCA
from sklearn.metrics import silhouette_score
import json
from typing import Dict, List, Any
from app.models.customer import Customer
from app.config import settings


class ClusteringService:
    """客户分群服务 - K-Means聚类"""

    def __init__(self, db: Session):
        self.db = db
        self._df = None
        self._scaler = StandardScaler()
        self._pca = PCA(n_components=3)
        self._kmeans = None
        self.n_clusters = 5

    def _get_dataframe(self) -> pd.DataFrame:
        if self._df is None:
            customers = self.db.query(Customer).all()
            self._df = pd.DataFrame([{
                "id": c.id,
                "credit_score": c.credit_score,
                "age": c.age,
                "tenure": c.tenure,
                "balance": c.balance,
                "num_products": c.num_products,
                "has_credit_card": c.has_credit_card,
                "is_active_member": c.is_active_member,
                "estimated_salary": c.estimated_salary,
                "exited": c.exited,
                "complain": c.complain,
                "satisfaction_score": c.satisfaction_score,
                "points_earned": c.points_earned,
                "balance_salary_ratio": c.balance_salary_ratio,
            } for c in customers])
        return self._df

    def _prepare_features(self, df: pd.DataFrame) -> np.ndarray:
        """准备聚类特征"""
        features = ["credit_score", "age", "tenure", "balance", "num_products",
                    "has_credit_card", "is_active_member", "estimated_salary",
                    "satisfaction_score", "points_earned", "balance_salary_ratio"]

        X = df[features].values
        X_scaled = self._scaler.fit_transform(X)
        return X_scaled

    def fit_kmeans(self, n_clusters: int = None) -> Dict[str, Any]:
        """训练K-Means模型"""
        if n_clusters is None:
            n_clusters = self.n_clusters

        df = self._get_dataframe()
        X_scaled = self._prepare_features(df)

        self._kmeans = KMeans(n_clusters=n_clusters, random_state=settings.RANDOM_STATE, n_init=10)
        df["cluster_id"] = self._kmeans.fit_predict(X_scaled)

        # PCA for 3D visualization
        X_pca = self._pca.fit_transform(X_scaled)
        df["pca_x"] = X_pca[:, 0]
        df["pca_y"] = X_pca[:, 1]
        df["pca_z"] = X_pca[:, 2]

        self._df = df

        # Calculate metrics
        inertia = self._kmeans.inertia_
        silhouette = silhouette_score(X_scaled, df["cluster_id"])

        _save_cluster_cache(self)

        return {
            "n_clusters": n_clusters,
            "inertia": round(inertia, 2),
            "silhouette_score": round(silhouette, 4)
        }

    def get_elbow_method(self) -> Dict[str, Any]:
        """肘部法则 - 确定最优K值"""
        if _cluster_cache.get("elbow"):
            return _cluster_cache["elbow"]

        df = self._get_dataframe()
        X_scaled = self._prepare_features(df)

        inertias = []
        silhouette_scores = []
        k_range = range(2, 11)

        for k in k_range:
            kmeans = KMeans(n_clusters=k, random_state=settings.RANDOM_STATE, n_init=10)
            labels = kmeans.fit_predict(X_scaled)
            inertias.append(round(kmeans.inertia_, 2))
            silhouette_scores.append(round(silhouette_score(X_scaled, labels), 4))

        result = {
            "k_range": list(k_range),
            "inertias": inertias,
            "silhouette_scores": silhouette_scores
        }

        _cluster_cache["elbow"] = result
        return result

    def get_cluster_profiles(self) -> Dict[str, Any]:
        """获取聚类画像"""
        df = self._get_dataframe()

        if "cluster_id" not in df.columns:
            self.fit_kmeans()

        profiles = []
        features = ["credit_score", "age", "tenure", "balance", "num_products",
                    "estimated_salary", "satisfaction_score", "is_active_member"]

        for cluster_id in sorted(df["cluster_id"].unique()):
            cluster_df = df[df["cluster_id"] == cluster_id]

            profile = {
                "cluster_id": int(cluster_id),
                "count": len(cluster_df),
                "churn_rate": round(cluster_df["exited"].mean() * 100, 2),
                "features": {}
            }

            for feature in features:
                profile["features"][feature] = {
                    "mean": round(cluster_df[feature].mean(), 2),
                    "std": round(cluster_df[feature].std(), 2)
                }

            profiles.append(profile)

        return {
            "clusters": profiles,
            "total_customers": len(df)
        }

    def get_3d_scatter_data(self) -> Dict[str, Any]:
        """获取3D散点图数据"""
        df = self._get_dataframe()

        if "pca_x" not in df.columns:
            self.fit_kmeans()

        scatter_data = []
        for cluster_id in sorted(df["cluster_id"].unique()):
            cluster_df = df[df["cluster_id"] == cluster_id]
            scatter_data.append({
                "cluster_id": int(cluster_id),
                "points": cluster_df[["pca_x", "pca_y", "pca_z", "exited"]].values.tolist()
            })

        return {
            "data": scatter_data,
            "explained_variance": self._pca.explained_variance_ratio_.tolist()
        }

    def assign_clusters_to_customers(self):
        """将聚类结果保存到数据库"""
        df = self._get_dataframe()

        if "cluster_id" not in df.columns:
            self.fit_kmeans()

        for _, row in df.iterrows():
            customer = self.db.query(Customer).filter(Customer.id == row["id"]).first()
            if customer:
                customer.cluster_id = int(row["cluster_id"])
        self.db.commit()

    def get_cluster_names(self) -> Dict[int, str]:
        """基于聚类特征自动命名"""
        profiles = self.get_cluster_profiles()
        names = {}

        for cluster in profiles["clusters"]:
            cid = cluster["cluster_id"]
            f = cluster["features"]
            churn = cluster["churn_rate"]
            balance = f["balance"]["mean"]
            products = f["num_products"]["mean"]
            active = f["is_active_member"]["mean"]
            salary = f["estimated_salary"]["mean"]

            # 组合多个维度判断
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


_cluster_cache = {
    "fitted": False,
    "kmeans": None,
    "df": None,
    "pca": None,
    "scaler": None,
    "elbow": None,
}


def get_clustering_service(db: Session) -> ClusteringService:
    service = ClusteringService(db)
    if _cluster_cache["fitted"]:
        service._kmeans = _cluster_cache["kmeans"]
        service._df = _cluster_cache["df"]
        service._pca = _cluster_cache["pca"]
        service._scaler = _cluster_cache["scaler"]
    return service


def _save_cluster_cache(service: ClusteringService):
    _cluster_cache["fitted"] = True
    _cluster_cache["kmeans"] = service._kmeans
    _cluster_cache["df"] = service._df
    _cluster_cache["pca"] = service._pca
    _cluster_cache["scaler"] = service._scaler
