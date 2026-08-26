"""成本收益分析服务 — 依赖已训练模型（从磁盘加载）。"""

import json
import numpy as np
from pathlib import Path
from sqlalchemy.orm import Session
from typing import Dict, Any

from app.config import settings
from app.services.data_loader import DataLoader, prepare_features
from app.services import risk_scoring
from sklearn.model_selection import train_test_split
import joblib

MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"


class CostBenefitService:
    """成本收益分析服务 — 从磁盘加载最佳模型进行分析。"""

    def __init__(self, db: Session):
        self.db = db

    def _load_best_model(self):
        """从磁盘加载最佳模型, 返回 (model, model_name, meta)。"""
        meta_path = MODEL_DIR / "meta.json"
        if not meta_path.exists():
            return None, None, None

        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        best_name = max(meta["results"].keys(), key=lambda x: meta["results"][x]["auc"])
        model_path = MODEL_DIR / (best_name.lower().replace(" ", "_") + ".joblib")

        if not model_path.exists():
            return None, None, None

        return joblib.load(model_path), best_name, meta

    def analyze_thresholds(self, cost_ratio: float = 5.0) -> Dict[str, Any]:
        """分析不同阈值下的成本收益。

        cost_ratio: 漏检成本/误报成本 的比值
                    例如 cost_ratio=5 表示漏掉一个流失客户的成本是误报的 5 倍
        """
        model, best_name, _ = self._load_best_model()
        if model is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}

        # 加载数据
        loader = DataLoader(self.db)
        df = loader.load_all()
        X, y, feature_names = prepare_features(df)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE,
            random_state=settings.RANDOM_STATE, stratify=y,
        )

        raw_proba = model.predict_proba(X_test)[:, 1]
        y_proba, _ = risk_scoring.calibrate_probs(self.db, raw_proba)

        thresholds = np.arange(0.05, 0.96, 0.05)
        results = []

        for threshold in thresholds:
            y_pred = (y_proba >= threshold).astype(int)

            tp = int(np.sum((y_pred == 1) & (y_test == 1)))
            fp = int(np.sum((y_pred == 1) & (y_test == 0)))
            fn = int(np.sum((y_pred == 0) & (y_test == 1)))
            tn = int(np.sum((y_pred == 0) & (y_test == 0)))

            benefit = tp * 1
            cost_fn = fn * cost_ratio
            cost_fp = fp * 1
            net_profit = benefit - cost_fn - cost_fp

            precision = tp / (tp + fp) if (tp + fp) > 0 else 0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

            results.append({
                "threshold": round(float(threshold), 2),
                "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
                "net_profit": round(float(net_profit), 2),
                "benefit": round(float(benefit), 2),
                "cost_fn": round(float(cost_fn), 2),
                "cost_fp": round(float(cost_fp), 2),
            })

        optimal = max(results, key=lambda x: x["net_profit"])

        return {
            "cost_ratio": cost_ratio,
            "best_model": best_name,
            "test_size": len(y_test),
            "churn_count": int(y_test.sum()),
            "thresholds": results,
            "optimal_threshold": optimal["threshold"],
            "optimal_metrics": optimal,
        }

    def get_business_summary(self) -> Dict[str, Any]:
        """业务摘要 — 年化成本收益，用于前端展示。"""
        analysis = self.analyze_thresholds(cost_ratio=5.0)
        if "error" in analysis:
            return analysis

        opt = analysis.get("optimal_metrics", {})

        total_customers = 10000
        annual_churn_rate = 0.2037
        avg_customer_value = 50000

        annual_churn_count = int(total_customers * annual_churn_rate)
        annual_loss = annual_churn_count * avg_customer_value

        retained_with_model = int(annual_churn_count * opt.get("recall", 0))
        reduced_loss = retained_with_model * avg_customer_value

        return {
            "total_customers": total_customers,
            "annual_churn_count": annual_churn_count,
            "annual_churn_rate": round(annual_churn_rate * 100, 2),
            "avg_customer_value": avg_customer_value,
            "annual_loss": annual_loss,
            "optimal_threshold": opt.get("threshold", 0.5),
            "model_recall": opt.get("recall", 0),
            "retained_customers": retained_with_model,
            "reduced_loss": reduced_loss,
            "roi": round(reduced_loss / (annual_loss * 0.1), 2) if annual_loss > 0 else 0,
        }


def get_cost_benefit_service(db: Session) -> CostBenefitService:
    return CostBenefitService(db)
