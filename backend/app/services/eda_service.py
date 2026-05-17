import pandas as pd
import numpy as np
from sqlalchemy.orm import Session
from app.models.customer import Customer
from typing import Dict, List, Any


class EDAService:
    """探索性数据分析服务"""

    def __init__(self, db: Session):
        self.db = db
        self._df = None

    def _get_dataframe(self) -> pd.DataFrame:
        if self._df is None:
            customers = self.db.query(Customer).all()
            self._df = pd.DataFrame([{
                "row_number": c.row_number,
                "customer_id": c.customer_id,
                "credit_score": c.credit_score,
                "geography": c.geography,
                "gender": c.gender,
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
                "card_type": c.card_type,
                "points_earned": c.points_earned,
                "age_group": c.age_group,
                "balance_salary_ratio": c.balance_salary_ratio,
            } for c in customers])
        return self._df

    def get_correlation_matrix(self) -> Dict[str, Any]:
        """计算相关性矩阵"""
        df = self._get_dataframe()
        numeric_cols = ["credit_score", "age", "tenure", "balance", "num_products",
                        "has_credit_card", "is_active_member", "estimated_salary",
                        "exited", "complain", "satisfaction_score", "points_earned"]

        corr_matrix = df[numeric_cols].corr()

        return {
            "features": numeric_cols,
            "matrix": corr_matrix.values.tolist()
        }

    def get_churn_analysis(self) -> Dict[str, Any]:
        """流失分析：流失客户 vs 留存客户对比"""
        df = self._get_dataframe()

        churned = df[df["exited"] == 1]
        retained = df[df["exited"] == 0]

        analysis = {
            "counts": {
                "churned": len(churned),
                "retained": len(retained)
            },
            "numeric_features": {}
        }

        numeric_features = ["age", "credit_score", "balance", "estimated_salary",
                            "tenure", "num_products", "satisfaction_score"]

        for feature in numeric_features:
            analysis["numeric_features"][feature] = {
                "churned": {
                    "mean": round(churned[feature].mean(), 2),
                    "median": round(churned[feature].median(), 2),
                    "std": round(churned[feature].std(), 2)
                },
                "retained": {
                    "mean": round(retained[feature].mean(), 2),
                    "median": round(retained[feature].median(), 2),
                    "std": round(retained[feature].std(), 2)
                }
            }

        return analysis

    def get_churn_by_category(self, feature: str) -> Dict[str, Any]:
        """按分类特征统计流失率"""
        df = self._get_dataframe()

        if feature not in ["geography", "gender", "num_products", "has_credit_card",
                           "is_active_member", "card_type", "age_group", "satisfaction_score"]:
            return {"error": f"Feature {feature} not supported"}

        result = df.groupby(feature).agg(
            total=("exited", "count"),
            churned=("exited", "sum")
        ).reset_index()

        result["churn_rate"] = round(result["churned"] / result["total"] * 100, 2)

        return {
            "feature": feature,
            "data": result.to_dict(orient="records")
        }

    def get_age_distribution(self) -> Dict[str, Any]:
        """年龄分布分析"""
        df = self._get_dataframe()

        bins = [0, 25, 35, 45, 55, 100]
        labels = ["18-25", "26-35", "36-45", "46-55", "56+"]
        df["age_group"] = pd.cut(df["age"], bins=bins, labels=labels)

        result = df.groupby("age_group", observed=True).agg(
            total=("exited", "count"),
            churned=("exited", "sum")
        ).reset_index()

        result["churn_rate"] = round(result["churned"] / result["total"] * 100, 2)

        return {
            "feature": "age_group",
            "data": result.to_dict(orient="records")
        }

    def get_product_overload_effect(self) -> Dict[str, Any]:
        """产品过载效应分析"""
        df = self._get_dataframe()

        result = df.groupby("num_products").agg(
            total=("exited", "count"),
            churned=("exited", "sum")
        ).reset_index()

        result["churn_rate"] = round(result["churned"] / result["total"] * 100, 2)

        return {
            "feature": "num_products",
            "data": result.to_dict(orient="records")
        }

    def get_numeric_distribution(self, feature: str) -> Dict[str, Any]:
        """数值特征分布"""
        df = self._get_dataframe()

        if feature not in df.columns:
            return {"error": f"Feature {feature} not found"}

        data = df[feature]

        return {
            "feature": feature,
            "statistics": {
                "mean": round(data.mean(), 2),
                "median": round(data.median(), 2),
                "std": round(data.std(), 2),
                "min": round(data.min(), 2),
                "max": round(data.max(), 2),
                "q25": round(data.quantile(0.25), 2),
                "q75": round(data.quantile(0.75), 2)
            },
            "histogram": {
                "bins": pd.cut(data, bins=20).value_counts().sort_index().index.tolist(),
                "counts": pd.cut(data, bins=20).value_counts().sort_index().values.tolist()
            }
        }

    def get_comprehensive_eda(self) -> Dict[str, Any]:
        """综合EDA分析"""
        df = self._get_dataframe()

        return {
            "overview": {
                "total_customers": len(df),
                "churned_customers": int(df["exited"].sum()),
                "churn_rate": round(df["exited"].mean() * 100, 2),
                "avg_age": round(df["age"].mean(), 1),
                "avg_balance": round(df["balance"].mean(), 2),
                "avg_salary": round(df["estimated_salary"].mean(), 2)
            },
            "correlation": self.get_correlation_matrix(),
            "churn_analysis": self.get_churn_analysis(),
            "churn_by_geography": self.get_churn_by_category("geography"),
            "churn_by_gender": self.get_churn_by_category("gender"),
            "churn_by_products": self.get_churn_by_category("num_products"),
            "age_distribution": self.get_age_distribution(),
            "product_overload": self.get_product_overload_effect()
        }


def get_eda_service(db: Session) -> EDAService:
    return EDAService(db)
