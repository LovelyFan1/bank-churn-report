import pandas as pd
import numpy as np
import json
import os
from pathlib import Path
from sqlalchemy.orm import Session
from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.metrics import (accuracy_score, precision_score, recall_score,
                             f1_score, roc_auc_score, roc_curve, confusion_matrix)
import xgboost as xgb
import lightgbm as lgb
import shap
import joblib
from typing import Dict, List, Any
from app.models.customer import Customer
from app.models.model_result import ModelResult
from app.config import settings


class ModelService:
    """ML模型训练服务 - 基于ImbPipeline封装"""

    def __init__(self, db: Session):
        self.db = db
        self._df = None
        self._models = {}
        self._results = {}
        self._feature_names = None

    def _get_dataframe(self) -> pd.DataFrame:
        if self._df is None:
            customers = self.db.query(Customer).all()
            self._df = pd.DataFrame([{
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
                "points_earned": c.points_earned,
            } for c in customers])
        return self._df

    def _prepare_features(self, df: pd.DataFrame):
        """准备特征和标签"""
        # 数值特征
        numeric_features = ["credit_score", "age", "tenure", "balance", "num_products",
                           "has_credit_card", "is_active_member", "estimated_salary",
                           "satisfaction_score", "points_earned"]

        # 类别特征（需要编码）
        categorical_features = ["geography", "gender"]

        # 目标变量
        y = df["exited"].values

        # 特征矩阵
        X_numeric = df[numeric_features].values

        # 类别特征编码
        geography_map = {"France": 0, "Germany": 1, "Spain": 2}
        gender_map = {"Male": 0, "Female": 1}

        X_geography = df["geography"].map(geography_map).values.reshape(-1, 1)
        X_gender = df["gender"].map(gender_map).values.reshape(-1, 1)

        # 合并特征
        X = np.hstack([X_numeric, X_geography, X_gender])

        # 特征名称
        feature_names = numeric_features + categorical_features
        self._feature_names = feature_names

        return X, y, feature_names

    def train_all_models(self) -> Dict[str, Any]:
        """训练所有模型并评估"""
        df = self._get_dataframe()
        X, y, feature_names = self._prepare_features(df)

        # 划分训练集和测试集
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE, random_state=settings.RANDOM_STATE, stratify=y
        )

        # 定义模型
        models = {
            "Logistic Regression": LogisticRegression(max_iter=1000, random_state=settings.RANDOM_STATE),
            "Random Forest": RandomForestClassifier(n_estimators=100, random_state=settings.RANDOM_STATE),
            "XGBoost": xgb.XGBClassifier(
                n_estimators=100,
                learning_rate=0.1,
                max_depth=5,
                random_state=settings.RANDOM_STATE,
                use_label_encoder=False,
                eval_metric="logloss"
            ),
            "LightGBM": lgb.LGBMClassifier(
                n_estimators=100,
                learning_rate=0.1,
                max_depth=5,
                random_state=settings.RANDOM_STATE,
                verbose=-1
            ),
            "GBDT": GradientBoostingClassifier(
                n_estimators=100,
                learning_rate=0.1,
                max_depth=5,
                random_state=settings.RANDOM_STATE
            )
        }

        results = {}

        for name, model in models.items():
            print(f"Training {name}...")

            # 训练模型
            model.fit(X_train, y_train)

            # 预测
            y_pred = model.predict(X_test)
            y_proba = model.predict_proba(X_test)[:, 1]

            # 计算指标
            accuracy = accuracy_score(y_test, y_pred)
            precision = precision_score(y_test, y_pred)
            recall = recall_score(y_test, y_pred)
            f1 = f1_score(y_test, y_pred)
            auc = roc_auc_score(y_test, y_proba)

            # ROC曲线数据（过滤inf值）
            fpr, tpr, thresholds = roc_curve(y_test, y_proba)
            roc_data = {
                "fpr": [0.0 if np.isinf(v) else round(float(v), 6) for v in fpr],
                "tpr": [0.0 if np.isinf(v) else round(float(v), 6) for v in tpr],
                "thresholds": [1.0 if np.isinf(v) else round(float(v), 6) for v in thresholds]
            }

            # 混淆矩阵
            cm = confusion_matrix(y_test, y_pred)
            confusion_data = {
                "tn": int(cm[0, 0]),
                "fp": int(cm[0, 1]),
                "fn": int(cm[1, 0]),
                "tp": int(cm[1, 1])
            }

            # 特征重要性
            if hasattr(model, "feature_importances_"):
                importance = model.feature_importances_.tolist()
            elif hasattr(model, "coef_"):
                importance = np.abs(model.coef_[0]).tolist()
            else:
                importance = [0] * len(feature_names)

            feature_importance = dict(zip(feature_names, importance))

            # 交叉验证
            cv = StratifiedKFold(n_splits=settings.CV_FOLDS, shuffle=True, random_state=settings.RANDOM_STATE)
            cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="roc_auc")

            # 处理inf值
            cv_mean = float(cv_scores.mean()) if not np.isinf(cv_scores.mean()) else 0.0
            cv_std = float(cv_scores.std()) if not np.isinf(cv_scores.std()) else 0.0

            results[name] = {
                "accuracy": round(accuracy, 4),
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1_score": round(f1, 4),
                "auc": round(auc, 4),
                "cv_auc_mean": round(cv_mean, 4),
                "cv_auc_std": round(cv_std, 4),
                "roc_curve": roc_data,
                "confusion_matrix": confusion_data,
                "feature_importance": feature_importance
            }

            # 保存模型
            self._models[name] = model
            self._results[name] = results[name]

            print(f"{name}: AUC={auc:.4f}, F1={f1:.4f}")

        _save_to_cache(self)
        return results

    def get_model_comparison(self) -> Dict[str, Any]:
        """获取模型对比结果"""
        if not self._results:
            self.train_all_models()

        comparison = []
        for name, result in self._results.items():
            comparison.append({
                "model_name": name,
                "accuracy": result["accuracy"],
                "precision": result["precision"],
                "recall": result["recall"],
                "f1_score": result["f1_score"],
                "auc": result["auc"],
                "cv_auc_mean": result["cv_auc_mean"],
                "cv_auc_std": result["cv_auc_std"]
            })

        # 按AUC排序
        comparison.sort(key=lambda x: x["auc"], reverse=True)

        return {
            "models": comparison,
            "best_model": comparison[0]["model_name"]
        }

    def get_roc_curves(self) -> Dict[str, Any]:
        """获取所有模型的ROC曲线数据"""
        if not self._results:
            self.train_all_models()

        curves = {}
        for name, result in self._results.items():
            curves[name] = result["roc_curve"]

        return {"curves": curves}

    def get_feature_importance(self) -> Dict[str, Any]:
        """获取特征重要性对比"""
        if not self._results:
            self.train_all_models()

        importance_data = {}
        for name, result in self._results.items():
            importance_data[name] = result["feature_importance"]

        return {
            "features": self._feature_names,
            "importance": importance_data
        }

    def get_confusion_matrices(self) -> Dict[str, Any]:
        """获取混淆矩阵"""
        if not self._results:
            self.train_all_models()

        matrices = {}
        for name, result in self._results.items():
            matrices[name] = result["confusion_matrix"]

        return {"matrices": matrices}

    def predict_single(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """单个客户预测"""
        if not self._models:
            self.train_all_models()

        # 使用最佳模型（AUC最高的）
        best_model_name = max(self._results.keys(), key=lambda x: self._results[x]["auc"])
        model = self._models[best_model_name]

        # 准备特征
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
            0 if customer_data.get("geography", "France") == "France" else
            1 if customer_data.get("geography", "France") == "Germany" else 2,
            0 if customer_data.get("gender", "Male") == "Male" else 1
        ]])

        # 预测
        prediction = model.predict(features)[0]
        probability = model.predict_proba(features)[0][1]

        # 风险等级
        if probability >= 0.7:
            risk_level = "CRITICAL"
            risk_color = "#ff4444"
        elif probability >= 0.3:
            risk_level = "HIGH"
            risk_color = "#ffaa00"
        elif probability >= 0.1:
            risk_level = "MEDIUM"
            risk_color = "#ffdd00"
        else:
            risk_level = "LOW"
            risk_color = "#44ff44"

        return {
            "prediction": int(prediction),
            "probability": round(probability, 4),
            "risk_level": risk_level,
            "risk_color": risk_color,
            "model_used": best_model_name
        }

    def batch_score_all(self, top_n: int = 100) -> Dict[str, Any]:
        """批量打分所有客户，返回 Top N 高风险客户 + 风险分布"""
        if not self._models:
            self.train_all_models()

        best_model_name = max(self._results.keys(), key=lambda x: self._results[x]["auc"])
        model = self._models[best_model_name]

        df = self._get_dataframe()
        X, y, feature_names = self._prepare_features(df)

        # 批量预测
        probabilities = model.predict_proba(X)[:, 1]

        # 构建客户风险列表
        customers = self.db.query(Customer).all()
        scored = []
        for i, c in enumerate(customers):
            prob = float(probabilities[i])
            if prob >= 0.7:
                risk_level = "CRITICAL"
            elif prob >= 0.3:
                risk_level = "HIGH"
            elif prob >= 0.1:
                risk_level = "MEDIUM"
            else:
                risk_level = "LOW"

            # 风险因素
            risk_factors = []
            if c.complain == 1:
                risk_factors.append("已投诉")
            if c.is_active_member == 0:
                risk_factors.append("非活跃")
            if c.num_products >= 3:
                risk_factors.append(f"{c.num_products}产品")
            if c.age >= 50:
                risk_factors.append("高龄")
            if c.balance == 0:
                risk_factors.append("零余额")
            if c.geography == "Germany":
                risk_factors.append("德国")

            scored.append({
                "id": c.id,
                "customer_id": c.customer_id,
                "surname": c.surname,
                "geography": c.geography,
                "age": c.age,
                "balance": round(c.balance, 2),
                "estimated_salary": round(c.estimated_salary, 2),
                "num_products": c.num_products,
                "is_active_member": c.is_active_member,
                "complain": c.complain,
                "probability": round(prob, 4),
                "risk_level": risk_level,
                "risk_factors": risk_factors,
            })

        # 按概率降序排序
        scored.sort(key=lambda x: x["probability"], reverse=True)

        # 风险分布统计
        dist = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for s in scored:
            dist[s["risk_level"]] += 1

        return {
            "total_customers": len(scored),
            "top_customers": scored[:top_n],
            "risk_distribution": dist,
            "model_used": best_model_name,
        }

    def get_shap_global(self) -> Dict[str, Any]:
        """全局SHAP特征重要性分析"""
        if _model_cache.get("shap_global"):
            return _model_cache["shap_global"]

        if not self._models:
            self.train_all_models()

        df = self._get_dataframe()
        X, y, feature_names = self._prepare_features(df)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE, random_state=settings.RANDOM_STATE, stratify=y
        )

        shap_results = {}
        for name, model in self._models.items():
            try:
                explainer = shap.TreeExplainer(model) if hasattr(model, 'feature_importances_') else shap.KernelExplainer(model.predict_proba, shap.sample(X_train, 100))
                shap_values = explainer.shap_values(X_test[:200])

                # For binary classification, shap_values may be a list [class_0, class_1]
                if isinstance(shap_values, list):
                    shap_values = shap_values[1]

                mean_abs_shap = np.abs(shap_values).mean(axis=0)
                importance = dict(zip(feature_names, [round(float(v), 6) for v in mean_abs_shap]))
                shap_results[name] = importance
            except Exception as e:
                print(f"SHAP error for {name}: {e}")
                shap_results[name] = {}

        result = {
            "features": feature_names,
            "shap_importance": shap_results
        }

        _model_cache["shap_global"] = result
        return result

    def get_shap_single(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """单客户SHAP预测解释"""
        if not self._models:
            self.train_all_models()

        best_model_name = max(self._results.keys(), key=lambda x: self._results[x]["auc"])
        model = self._models[best_model_name]

        df = self._get_dataframe()
        X, y, feature_names = self._prepare_features(df)
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE, random_state=settings.RANDOM_STATE, stratify=y
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
            0 if customer_data.get("geography", "France") == "France" else
            1 if customer_data.get("geography", "France") == "Germany" else 2,
            0 if customer_data.get("gender", "Male") == "Male" else 1
        ]])

        try:
            explainer = shap.TreeExplainer(model) if hasattr(model, 'feature_importances_') else shap.KernelExplainer(model.predict_proba, shap.sample(X_train, 100))
            shap_values = explainer.shap_values(features)

            if isinstance(shap_values, list):
                shap_values = shap_values[1]

            shap_vals = shap_values[0]
            contribution = dict(zip(feature_names, [round(float(v), 6) for v in shap_vals]))

            # Sort by absolute contribution
            sorted_contrib = sorted(contribution.items(), key=lambda x: abs(x[1]), reverse=True)

            return {
                "model_used": best_model_name,
                "base_value": round(float(explainer.expected_value[1] if isinstance(explainer.expected_value, list) else explainer.expected_value), 4),
                "contributions": contribution,
                "top_factors": [{"feature": f, "impact": v, "direction": "positive" if v > 0 else "negative"} for f, v in sorted_contrib[:5]]
            }
        except Exception as e:
            return {"error": str(e)}

    def save_models(self):
        """保存模型到文件"""
        for name, model in self._models.items():
            filename = f"models/{name.lower().replace(' ', '_')}.joblib"
            joblib.dump(model, filename)

    def save_results_to_db(self):
        """保存模型结果到数据库"""
        if not self._results:
            self.train_all_models()

        for name, result in self._results.items():
            model_result = ModelResult(
                model_name=name,
                auc=result["auc"],
                accuracy=result["accuracy"],
                precision_score=result["precision"],
                recall=result["recall"],
                f1_score=result["f1_score"],
                training_time=0,
                roc_curve_data=json.dumps(result["roc_curve"]),
                feature_importance=json.dumps(result["feature_importance"])
            )
            self.db.add(model_result)

        self.db.commit()


_model_cache = {
    "trained": False,
    "models": {},
    "results": {},
    "feature_names": None,
    "df": None,
    "shap_global": None,
}

# 模型持久化目录
MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"


def get_model_service(db: Session) -> ModelService:
    service = ModelService(db)
    if _model_cache["trained"]:
        service._models = _model_cache["models"]
        service._results = _model_cache["results"]
        service._feature_names = _model_cache["feature_names"]
        service._df = _model_cache["df"]
    return service


def _save_to_cache(service: ModelService):
    _model_cache["trained"] = True
    _model_cache["models"] = service._models
    _model_cache["results"] = service._results
    _model_cache["feature_names"] = service._feature_names
    _model_cache["df"] = service._df
    # 持久化到磁盘
    _save_to_disk(service)


def _save_to_disk(service: ModelService):
    """将模型和元数据保存到磁盘"""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    # 保存每个模型
    for name, model in service._models.items():
        filename = name.lower().replace(" ", "_") + ".joblib"
        joblib.dump(model, MODEL_DIR / filename)

    # 保存元数据（结果、特征名）
    meta = {
        "results": service._results,
        "feature_names": service._feature_names,
    }
    with open(MODEL_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    print(f"Models saved to {MODEL_DIR}")


def _load_from_disk() -> bool:
    """从磁盘加载模型，成功返回 True"""
    meta_path = MODEL_DIR / "meta.json"
    if not meta_path.exists():
        return False

    try:
        # 加载元数据
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        # 加载所有模型文件
        models = {}
        for name in meta["results"].keys():
            filename = name.lower().replace(" ", "_") + ".joblib"
            model_path = MODEL_DIR / filename
            if model_path.exists():
                models[name] = joblib.load(model_path)
            else:
                print(f"Model file not found: {model_path}")
                return False

        # 写入缓存
        _model_cache["trained"] = True
        _model_cache["models"] = models
        _model_cache["results"] = meta["results"]
        _model_cache["feature_names"] = meta["feature_names"]

        print(f"Loaded {len(models)} models from {MODEL_DIR}")
        return True

    except Exception as e:
        print(f"Failed to load models from disk: {e}")
        return False


# 启动时尝试从磁盘加载
_load_from_disk()
