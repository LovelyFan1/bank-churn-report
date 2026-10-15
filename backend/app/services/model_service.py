"""模型服务 — 纯读取 + 单条预测。

重算力任务（训练、SHAP、批量预测）已迁移到 Celery Worker:
    app/celery_tasks/train.py
    app/celery_tasks/predict.py

本服务只负责:
- 从磁盘/DB 读取已训练的模型和评估结果
- 单条实时预测（加载模型 → infer → 返回）
"""

import json
import logging
import time
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

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"


class ModelService:
    """模型读取 + 单条预测服务 — 无全局状态，线程安全。"""

    def __init__(self, db: Session):
        self.db = db
        # 已加载的模型对象，按需逐个填充（键 = 模型名）。
        # ⚠ 不预加载全部 —— Random Forest 实测 171.8 MB / 1,629 ms，
        # 而单条预测只需要最优的那一个（约 30 ms）。
        self._models: dict = {}
        self._meta: Optional[dict] = None

    # ── 模型加载（分两级，惰性）───────────────────────────
    #
    # 本文件 5 个读取接口只要 meta.json，却因为统一走 _ensure_loaded()
    # 而把 5 个模型文件全加载一遍。实测（10 万数据源，容器内）：
    #     读 meta.json(56 KB)                     12 ms
    #     额外加载 5 个模型                    2,756 ms
    #       └ Random Forest 171.8 MB             1,629 ms  ← 随训练数据量增长
    #       └ Logistic Regression                1,015 ms  ← sklearn 反序列化开销
    # 故拆成 _ensure_meta()（读元数据）与 _ensure_loaded()（读模型对象）。

    def _ensure_meta(self) -> bool:
        from app.services.model_manifest import resolve
        try:
            manifest = resolve(MODEL_DIR)
            previous = getattr(self, "_manifest", None)
            if previous is None or previous.revision != manifest.revision:
                self._models.clear()
            self._manifest = manifest
            self._meta = manifest.meta
            return True
        except Exception as error:
            self._meta = None
            self._manifest = None
            self._models.clear()
            logger.warning("模型清单不可用：%s", error)
            return False

    def get_best_model_name(self) -> Optional[str]:
        if not self._ensure_meta():
            return None
        return risk_scoring.select_model(self._meta)

    def get_best_model(self):
        """取 AUC 最高的模型对象。

        ⚠ 只加载**最优的那一个**，不调 _ensure_loaded() 全量加载 ——
        最优模型通常是 XGBoost/LightGBM（0.2~0.3 MB，加载约 30 ms），
        而全量加载会连带读入 Random Forest（171.8 MB，1,629 ms）。
        单条预测接口此前因此白等 1.6 秒（实测）。
        self._models 仍作为「已加载的模型」缓存，按需逐个填充。
        """
        if not self._ensure_meta():
            return None
        name = self.get_best_model_name()
        if not name:
            return None
        if name not in self._models:
            try:
                self._models[name] = joblib.load(self._manifest.path)
            except Exception as error:
                self._models.clear()
                logger.warning("模型文件无法加载：%s", error)
                return None
        return self._models.get(name)

    # ── 读取评估结果（从磁盘 meta.json）───────────────────

    # 下面 5 个接口的数据全部来自 meta.json，
    # 只走 _ensure_meta()（12 ms），不加载模型对象（2,756 ms）。

    def _engine_metrics_for_best(self) -> Optional[Dict[str, Any]]:
        """取「当前引擎口径」下最优模型的测试集指标（可能为 None）。

        ⚠ 为什么需要这个覆盖（实测缺陷，勿删）：

          meta.json 里的 recall/precision 是**训练当时**用当时的决策阈值算的，
          而决策阈值由 `net_profit` 决定 —— 后者依赖 COST_RATIO 与
          RETENTION_SUCCESS_RATE 两个可配置假设。任一假设被调整，
          阈值就会移动，meta.json 里的指标立即**过时**，直到下次重训。

          实测（改动 RETENTION_SUCCESS_RATE 引入 0.30 后，未重训）：
              /api/model/comparison   → LightGBM recall 0.7811（meta.json，旧阈值 0.20）
              /api/model/risk-info    → recall 0.2751（引擎，新阈值 0.60）
          同一模型两个召回率 —— 正是本项目反复出现的缺陷类型。

          仅在「重训」时才更新 meta.json 是治标：只要有人调一次假设参数
          （或换了成本口径），不一致就会重现。故此处做**运行时对齐**：
          最优模型的行以引擎口径为准，其余模型保留 meta 值（引擎只算最优那个）
          并明确标注 basis，读者能看出两行的口径不同。
        """
        try:
            from app.services import risk_scoring
            engine = risk_scoring._ensure_engine(self.db)
            if engine is None:
                return None
            dm = risk_scoring.evaluate_at_decision_threshold() or {}
            if not dm:
                return None
            return {
                "model_name": engine.get("name"),
                "threshold": dm.get("threshold"),
                "precision": dm.get("precision"),
                "recall": dm.get("recall"),
                "f1_score": dm.get("f1_score"),
                "accuracy": dm.get("accuracy"),
                "basis": "engine_at_current_decision_threshold",
            }
        except Exception:
            return None

    def get_model_comparison(self) -> Dict[str, Any]:
        if not self._ensure_meta():
            return {"models": [], "best_model": None, "error": "模型尚未训练，请先调用 POST /api/model/train"}

        # 引擎口径（若可用）—— 用于修正最优模型那一行
        live = self._engine_metrics_for_best()
        live_name = live["model_name"] if live else None

        comparison = []
        for name, result in self._meta["results"].items():
            row = {
                "model_name": name,
                "accuracy": result["accuracy"],
                "precision": result["precision"],
                "recall": result["recall"],
                "f1_score": result["f1_score"],
                "auc": result["auc"],
                "cv_auc_mean": result.get("cv_auc_mean", 0),
                "cv_auc_std": result.get("cv_auc_std", 0),
                # 口径标注：这两行来自 meta.json（训练当时）；最优那行会被覆盖
                "basis": "meta_json_at_training_time",
            }
            if live and name == live_name:
                # 用引擎口径覆盖 —— 保证与 /risk-info、/cost-benefit 一致
                row.update({
                    "precision": live["precision"],
                    "recall": live["recall"],
                    "f1_score": live["f1_score"],
                    "accuracy": live["accuracy"],
                    "threshold": live["threshold"],
                    "basis": live["basis"],
                })
            comparison.append(row)

        comparison.sort(key=lambda x: x["auc"], reverse=True)

        return {
            "models": comparison,
            "best_model": self._manifest.best_model,
            "model_version": self._manifest.revision,
            "manifest_status": self._manifest.status,
            "selection_basis": self._meta.get('selection_basis', 'legacy_cv_or_declared'),
            # 说明为什么最优那行的 recall 可能与 meta.json 不同源
            "note": (
                "最优模型行取自**当前引擎口径**（决策阈值随 COST_RATIO 与"
                " RETENTION_SUCCESS_RATE 变动，故与训练时写入 meta.json 的值可能不同）；"
                "其余模型行为训练时的 meta.json 值。每行 basis 字段标明各自口径。"
            ) if live else None,
            "live_metrics": live,
        }

    def get_roc_curves(self) -> Dict[str, Any]:
        if not self._ensure_meta():
            return {"curves": {}, "error": "模型尚未训练"}

        curves = {}
        for name, result in self._meta["results"].items():
            from app.services.curve_sampling import simplify
            curves[name] = simplify(result.get("roc_curve", {}))
        return {"curves": curves, "model_version": self._manifest.revision, "metrics_unchanged": True}

    def get_feature_importance(self) -> Dict[str, Any]:
        if not self._ensure_meta():
            return {"features": [], "importance": {}, "error": "模型尚未训练"}

        importance_data = {}
        for name, result in self._meta["results"].items():
            importance_data[name] = result.get("feature_importance", {})

        return {
            "features": self._meta.get("feature_names", FEATURE_NAMES),
            "importance": importance_data,
            "model_version": self._manifest.revision,
            "best_model": self._manifest.best_model,
        }

    def get_confusion_matrices(self) -> Dict[str, Any]:
        if not self._ensure_meta():
            return {"matrices": {}, "error": "模型尚未训练"}

        matrices = {}
        for name, result in self._meta["results"].items():
            matrices[name] = result.get("confusion_matrix", {})
        return {"matrices": matrices}

    def get_shap_global(self) -> Dict[str, Any]:
        """全局 SHAP 特征重要性 — 从磁盘读取（训练时已计算并持久化）。"""
        if not self._ensure_meta():
            return {"features": [], "shap_importance": {}, "error": "模型尚未训练"}
        return {
            "features": self._meta.get("feature_names", FEATURE_NAMES),
            "shap_importance": self._meta.get("shap_global", {}),
            "model_version": self._manifest.revision,
            "best_model": self._manifest.best_model,
        }

    # ── 单条预测（轻量，API 直接调用）─────────────────────

    def predict_single(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """单客户实时预测 — 加载模型后推理，毫秒级。"""
        model = self.get_best_model()
        if model is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}

        from app.schemas.prediction import feature_vector
        features = feature_vector(customer_data)

        from app.services.shap_contract import positive_class
        raw_probability = float(model.predict_proba(features)[0][positive_class(model)])
        probability, risk_level = risk_scoring.classify_single(self.db, raw_probability)

        info = risk_scoring.get_risk_info()
        if info.get("model_version") != self._manifest.revision:
            raise ValueError("模型快照已切换，请重试")
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
            "model_used": self._manifest.best_model,
            "model_version": self._manifest.revision,
            "manifest_status": self._manifest.status,
            "optimal_threshold": optimal_threshold,
        }

    # ── SHAP 单条解释（轻量，加载模型后推理）───────────────

    def get_shap_single(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """单客户 SHAP 解释 — 需要加载模型和部分训练数据做背景。"""
        import shap

        model = self.get_best_model()
        if model is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}
        engine = risk_scoring._ensure_engine(self.db)
        if engine is None or engine['model_version'] != self._manifest.revision:
            return {'error': '模型和数据快照不匹配', 'basis': 'unavailable'}

        from app.services.data_loader import prepare_features, get_cached_customer_df
        from sklearn.model_selection import train_test_split

        # 走共享缓存 —— 此前这里是 `DataLoader(self.db).load_all()`，
        # 10 万行实测 2.8 秒/次，而 SHAP 只需要一份**采样**做背景。
        # 注意：下面的 split 仍按全量做，是为了与训练时的划分保持同一索引口径。
        df = get_cached_customer_df(self.db)
        X, y, _ = prepare_features(df)
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE,
            random_state=settings.RANDOM_STATE, stratify=y,
        )

        from app.schemas.prediction import feature_vector
        features = feature_vector(customer_data)

        try:
            from app.services.shap_contract import explain
            explanation = explain(model, features, background=X_train)
            shap_values = explanation["values"]

            shap_vals = shap_values[0]
            contribution = dict(zip(FEATURE_NAMES, [round(float(v), 6) for v in shap_vals]))
            sorted_contrib = sorted(contribution.items(), key=lambda x: abs(x[1]), reverse=True)

            base_value = explanation["base_value"]

            return {
                "model_used": self._manifest.best_model,
            "model_version": self._manifest.revision,
            "manifest_status": self._manifest.status,
                "base_value": round(float(base_value), 4),
                "basis": "model_shap",
                "output_space": explanation["output_space"],
                "positive_class": 1,
                "additivity_checked": True,
                "contributions": contribution,
                "top_factors": [
                    {"feature": f, "impact": v, "direction": "positive" if v > 0 else "negative"}
                    for f, v in sorted_contrib[:5]
                ],
            }
        except Exception as e:
            return {"error": "模型解释未能通过支持性或贡献加和检查", "basis": "unavailable", "reason": type(e).__name__}


def get_model_service(db: Session) -> ModelService:
    """工厂函数 — 每次创建新的 ModelService（无共享状态）。"""
    return ModelService(db)
