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
        """只加载 meta.json —— 评估指标 / ROC / 特征重要性 / 混淆矩阵都在里面。

        只读 meta 比全加载快约 222 倍。此前 4 个接口因此各白慢 1.5 秒。

        ⚠ 失败处理（本函数曾是"故障隐形"的元凶）：
          旧实现是 `except Exception: return False` —— 把 json.load 的所有异常
          吞掉，于是模型文件正在被重写、读到半截 JSON 时，接口**静默返回**
          {"error": "模型尚未训练"} 且 HTTP 200。日志里连一条 500 都没有，
          运维无从察觉，前端只表现为"页面只剩空框"。

          现在：真实不存在（文件缺失）才直接返回 False；读取/解析失败属于
          "可能是写入窗口或文件损坏"，做几次短重试并记录 warning。写入端已改为
          原子替换（见 celery_tasks/train.py 的 _atomic_write_bytes），
          正常情况下这里不会再失败；重试是为磁盘/网络挂载抖动的兜底。
        """
        if self._meta is not None:
            return True

        meta_path = MODEL_DIR / "meta.json"
        if not meta_path.exists():
            # 真的没有模型 —— 这是合法的"尚未训练"，不算异常
            return False

        # 读取失败大概率是撞上了训练写入窗口，短暂退避重试
        ATTEMPTS = 3
        last_err: Optional[Exception] = None
        for attempt in range(ATTEMPTS):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    self._meta = json.load(f)
                return True
            except (json.JSONDecodeError, ValueError, OSError) as e:
                last_err = e
                if attempt < ATTEMPTS - 1:
                    time.sleep(0.15 * (attempt + 1))

        logger.warning(
            "读取 %s 失败（已重试 %d 次）: %s: %s —— "
            "该次请求将退化为「模型未就绪」，请检查是否有训练任务正在写入",
            meta_path, ATTEMPTS, type(last_err).__name__, last_err,
        )
        return False

    def get_best_model_name(self) -> Optional[str]:
        if not self._ensure_meta():
            return None
        return max(self._meta["results"].keys(), key=lambda x: self._meta["results"][x]["auc"])

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
            path = MODEL_DIR / (name.lower().replace(" ", "_") + ".joblib")
            if not path.exists():
                return None
            self._models[name] = joblib.load(path)
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
            "best_model": comparison[0]["model_name"],
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
            curves[name] = result.get("roc_curve", {})
        return {"curves": curves}

    def get_feature_importance(self) -> Dict[str, Any]:
        if not self._ensure_meta():
            return {"features": [], "importance": {}, "error": "模型尚未训练"}

        importance_data = {}
        for name, result in self._meta["results"].items():
            importance_data[name] = result.get("feature_importance", {})

        return {
            "features": self._meta.get("feature_names", FEATURE_NAMES),
            "importance": importance_data,
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
