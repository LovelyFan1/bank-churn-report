"""模型服务 — 纯读取 + 单条预测。

重算力任务（训练、SHAP、批量预测）已迁移到 Celery Worker:
    app/celery_tasks/train.py
    app/celery_tasks/predict.py

本服务只负责:
- 从磁盘/DB 读取已训练的模型和评估结果
- 单条实时预测（加载模型 → infer → 返回）
"""

import json
import numpy as np
import joblib
from pathlib import Path
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional

from app.models.customer import Customer
from app.models.model_result import ModelResult
from app.config import settings
from app.services.data_loader import (
    GEOGRAPHY_MAP, GENDER_MAP,
    FEATURE_NAMES, NUMERIC_FEATURES,
)
from app.services import risk_scoring

MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"


class ModelService:
    """模型读取 + 单条预测服务 — 无全局状态，线程安全。"""

    def __init__(self, db: Session):
        self.db = db
        self._models: Optional[dict] = None
        self._meta: Optional[dict] = None

    # ── 模型加载（惰性）───────────────────────────────────

    def _ensure_loaded(self) -> bool:
        """从磁盘加载模型和元数据。返回 True 表示加载成功。"""
        if self._models is not None:
            return True

        meta_path = MODEL_DIR / "meta.json"
        if not meta_path.exists():
            return False

        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                self._meta = json.load(f)

            self._models = {}
            for name in self._meta["results"].keys():
                filepath = MODEL_DIR / (name.lower().replace(" ", "_") + ".joblib")
                if filepath.exists():
                    self._models[name] = joblib.load(filepath)

            return len(self._models) > 0
        except Exception:
            return False

    @property
    def is_trained(self) -> bool:
        return self._ensure_loaded()

    def get_best_model_name(self) -> Optional[str]:
        if not self._ensure_loaded():
            return None
        return max(self._meta["results"].keys(), key=lambda x: self._meta["results"][x]["auc"])

    def get_best_model(self):
        if not self._ensure_loaded():
            return None
        name = self.get_best_model_name()
        return self._models.get(name) if name else None

    # ── 读取评估结果（从磁盘 meta.json）───────────────────

    def get_model_comparison(self) -> Dict[str, Any]:
        if not self._ensure_loaded():
            return {"models": [], "best_model": None, "error": "模型尚未训练，请先调用 POST /api/model/train"}

        comparison = []
        for name, result in self._meta["results"].items():
            comparison.append({
                "model_name": name,
                "accuracy": result["accuracy"],
                "precision": result["precision"],
                "recall": result["recall"],
                "f1_score": result["f1_score"],
                "auc": result["auc"],
                "cv_auc_mean": result.get("cv_auc_mean", 0),
                "cv_auc_std": result.get("cv_auc_std", 0),
            })

        comparison.sort(key=lambda x: x["auc"], reverse=True)

        return {
            "models": comparison,
            "best_model": comparison[0]["model_name"],
        }

    def get_roc_curves(self) -> Dict[str, Any]:
        if not self._ensure_loaded():
            return {"curves": {}, "error": "模型尚未训练"}

        curves = {}
        for name, result in self._meta["results"].items():
            curves[name] = result.get("roc_curve", {})
        return {"curves": curves}

    def get_feature_importance(self) -> Dict[str, Any]:
        if not self._ensure_loaded():
            return {"features": [], "importance": {}, "error": "模型尚未训练"}

        importance_data = {}
        for name, result in self._meta["results"].items():
            importance_data[name] = result.get("feature_importance", {})

        return {
            "features": self._meta.get("feature_names", FEATURE_NAMES),
            "importance": importance_data,
        }

    def get_confusion_matrices(self) -> Dict[str, Any]:
        if not self._ensure_loaded():
            return {"matrices": {}, "error": "模型尚未训练"}

        matrices = {}
        for name, result in self._meta["results"].items():
            matrices[name] = result.get("confusion_matrix", {})
        return {"matrices": matrices}

    def get_shap_global(self) -> Dict[str, Any]:
        """全局 SHAP 特征重要性 — 从磁盘读取（训练时已计算并持久化）。"""
        if not self._ensure_loaded():
            return {"features": [], "shap_importance": {}, "error": "模型尚未训练"}
        return {
            "features": self._meta.get("feature_names", FEATURE_NAMES),
            "shap_importance": self._meta.get("shap_global", {}),
        }

    # ── 单条预测（轻量，API 直接调用）─────────────────────

    def predict_single(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """单客户实时预测 — 加载模型后推理，毫秒级。"""
        model = self.get_best_model()
        if model is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}

        features = np.array([[
            customer_data.get("credit_score", 650),
            customer_data.get("age", 39),
            customer_data.get("tenure", 5),
            customer_data.get("balance", 76000),
            customer_data.get("num_products", 2),
            customer_data.get("has_credit_card", 1),
            customer_data.get("is_active_member", 1),
            customer_data.get("estimated_salary", 100000),
            customer_data.get("satisfaction_score", 3),
            customer_data.get("points_earned", 500),
            GEOGRAPHY_MAP.get(customer_data.get("geography", "France"), 0),
            GENDER_MAP.get(customer_data.get("gender", "Male"), 0),
        ]])

        raw_probability = float(model.predict_proba(features)[0][1])
        probability, risk_level = risk_scoring.classify_single(self.db, raw_probability)

        info = risk_scoring.get_risk_info()
        optimal_threshold = info.get("optimal_threshold") or 0.5
        prediction = 1 if probability >= optimal_threshold else 0

        risk_color = {
            "CRITICAL": "#ff4444", "HIGH": "#ffaa00",
            "MEDIUM": "#ffdd00", "LOW": "#44ff44",
        }.get(risk_level, "#44ff44")

        return {
            "prediction": prediction,
            "probability": probability,
            "risk_level": risk_level,
            "risk_color": risk_color,
            "model_used": self.get_best_model_name(),
            "optimal_threshold": optimal_threshold,
        }

    # ── SHAP 单条解释（轻量，加载模型后推理）───────────────

    def get_shap_single(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """单客户 SHAP 解释 — 需要加载模型和部分训练数据做背景。"""
        import shap

        model = self.get_best_model()
        if model is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}

        from app.services.data_loader import DataLoader, prepare_features
        from sklearn.model_selection import train_test_split

        loader = DataLoader(self.db)
        df = loader.load_all()
        X, y, _ = prepare_features(df)
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE,
            random_state=settings.RANDOM_STATE, stratify=y,
        )

        features = np.array([[
            customer_data.get("credit_score", 650),
            customer_data.get("age", 39),
            customer_data.get("tenure", 5),
            customer_data.get("balance", 76000),
            customer_data.get("num_products", 2),
            customer_data.get("has_credit_card", 1),
            customer_data.get("is_active_member", 1),
            customer_data.get("estimated_salary", 100000),
            customer_data.get("satisfaction_score", 3),
            customer_data.get("points_earned", 500),
            GEOGRAPHY_MAP.get(customer_data.get("geography", "France"), 0),
            GENDER_MAP.get(customer_data.get("gender", "Male"), 0),
        ]])

        try:
            if hasattr(model, "feature_importances_"):
                explainer = shap.TreeExplainer(model)
            else:
                explainer = shap.KernelExplainer(
                    model.predict_proba,
                    shap.sample(X_train, min(100, len(X_train))),
                )
            shap_values = explainer.shap_values(features)

            if isinstance(shap_values, list):
                shap_values = shap_values[1]

            shap_vals = shap_values[0]
            contribution = dict(zip(FEATURE_NAMES, [round(float(v), 6) for v in shap_vals]))
            sorted_contrib = sorted(contribution.items(), key=lambda x: abs(x[1]), reverse=True)

            base_value = explainer.expected_value
            if isinstance(base_value, list):
                base_value = base_value[1]

            return {
                "model_used": self.get_best_model_name(),
                "base_value": round(float(base_value), 4),
                "contributions": contribution,
                "top_factors": [
                    {"feature": f, "impact": v, "direction": "positive" if v > 0 else "negative"}
                    for f, v in sorted_contrib[:5]
                ],
            }
        except Exception as e:
            return {"error": str(e)}


def get_model_service(db: Session) -> ModelService:
    """工厂函数 — 每次创建新的 ModelService（无共享状态）。"""
    return ModelService(db)
