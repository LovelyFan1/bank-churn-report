"""模型训练 Celery 任务 — 在独立 Worker 进程中执行。

包含:
- train_all_models: 训练全部 5 个模型 + 交叉验证 + SHAP + 保存
"""

import json
import os
import numpy as np
from pathlib import Path
from celery.exceptions import SoftTimeLimitExceeded

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.customer import Customer
from app.models.model_result import ModelResult
from app.services.data_loader import DataLoader, prepare_features, FEATURE_NAMES
from app.config import settings

from sklearn.model_selection import train_test_split, cross_val_score, StratifiedKFold
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score,
    f1_score, roc_auc_score, roc_curve, confusion_matrix,
)
import xgboost as xgb
import lightgbm as lgb
import joblib

# 模型保存目录
MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"


def _get_models():
    """返回模型字典 — 独立函数方便测试和复用。"""
    return {
        "Logistic Regression": LogisticRegression(
            max_iter=2000, random_state=settings.RANDOM_STATE
        ),
        "Random Forest": RandomForestClassifier(
            n_estimators=100, random_state=settings.RANDOM_STATE, n_jobs=-1
        ),
        "XGBoost": xgb.XGBClassifier(
            n_estimators=100, learning_rate=0.1, max_depth=5,
            random_state=settings.RANDOM_STATE, use_label_encoder=False,
            eval_metric="logloss",
        ),
        "LightGBM": lgb.LGBMClassifier(
            n_estimators=100, learning_rate=0.1, max_depth=5,
            random_state=settings.RANDOM_STATE, verbose=-1,
        ),
        "GBDT": GradientBoostingClassifier(
            n_estimators=100, learning_rate=0.1, max_depth=5,
            random_state=settings.RANDOM_STATE,
        ),
    }


def _best_threshold_on_train(model, X_train, y_train) -> float:
    """在**训练集**上求净收益最大的概率切点（决策阈值）。

    ⚠ 为什么在训练集上选（而不是测试集）：
      如果在测试集上挑最优阈值、又在同一测试集上报告指标，阈值本身
      拟合了这批数据，指标会带**乐观偏差**、经不起追问。
      正确做法是训练集选阈值、测试集评估，两侧不重叠。

    ⚠ 与 risk_scoring.net_profit 同口径（统一走那个函数，不再本地复制公式）。
      口径自引入「挽留成功率 s」后的变化（见 config.RETENTION_SUCCESS_RATE）：
          TP 记 +(s × cost_ratio − 1)，FP 记 -1，FN 记 0
      此前 TP 记 +(cost_ratio − 1)，等价于 s=1.0，会把阈值选得**偏松**
      （名单偏大）。实测 s 从 1.0 降到 0.30 时，最优阈值从 0.20 升到 0.60。

      ⚠ 这里必须与 risk_scoring 用同一个函数而不是各写一份 ——
        两处若口径漂移，会出现「meta.json 说阈值 0.2、线上按 0.6 运营」
        这类无法对账的情况（正是本项目反复出现的缺陷类型）。
    """
    from app.services import risk_scoring

    p = model.predict_proba(X_train)[:, 1]
    best_t, best_profit = 0.5, -float("inf")
    for t in np.arange(0.05, 0.96, 0.05):
        pred = (p >= t).astype(int)
        tp = int(np.sum((pred == 1) & (y_train == 1)))
        fp = int(np.sum((pred == 1) & (y_train == 0)))
        fn = int(np.sum((pred == 0) & (y_train == 1)))
        profit = risk_scoring.net_profit(tp, fp, fn)
        if profit > best_profit:
            best_profit, best_t = profit, float(t)
    return best_t


def _train_and_evaluate(model, name: str, X_train, X_test, y_train, y_test) -> dict:
    """训练单个模型并返回评估指标。

    ⚠ 指标口径（本次修复的核心，此前这里是「同一召回率两个数」的源头）：
      旧实现用 `model.predict(X_test)` —— 即 sklearn 默认的 0.5 阈值算出
      recall=0.3627 并写进 meta.json，被 Dashboard / ModelComparison /
      InterventionStrategy 三处展示；而系统实际运营（客户列表排序、
      建单建议、cost_benefit）依据的是决策阈值(0.20)，对应 recall≈0.78。
      同一个系统里两个「模型召回率」，答辩时无法自圆其说。

      现改为：**主指标按决策阈值计算**（因为那才是"按净收益最优挑客户"
      时真实会发生的结果），同时保留默认 0.5 阈值下的指标供对照，
      字段名加 `at_default` 后缀，避免混淆。
    """
    model.fit(X_train, y_train)

    # ── 决策阈值：训练集选 ─────────────────────────────────
    decision_thr = _best_threshold_on_train(model, X_train, y_train)
    y_proba = model.predict_proba(X_test)[:, 1]
    y_pred = (y_proba >= decision_thr).astype(int)      # 主口径
    y_pred_def = (y_proba >= 0.5).astype(int)           # 对照口径

    # ROC 曲线
    fpr, tpr, thresholds = roc_curve(y_test, y_proba)
    roc_data = {
        "fpr": [0.0 if np.isinf(v) else round(float(v), 6) for v in fpr],
        "tpr": [0.0 if np.isinf(v) else round(float(v), 6) for v in tpr],
        "thresholds": [1.0 if np.isinf(v) else round(float(v), 6) for v in thresholds],
    }

    # 混淆矩阵 —— 按决策阈值（与主指标同口径）
    cm = confusion_matrix(y_test, y_pred)
    confusion_data = {
        "tn": int(cm[0, 0]), "fp": int(cm[0, 1]),
        "fn": int(cm[1, 0]), "tp": int(cm[1, 1]),
    }

    # 特征重要性
    if hasattr(model, "feature_importances_"):
        importance = model.feature_importances_.tolist()
    elif hasattr(model, "coef_"):
        importance = np.abs(model.coef_[0]).tolist()
    else:
        importance = [0] * len(FEATURE_NAMES)
    feature_importance = dict(zip(FEATURE_NAMES, importance))

    # 交叉验证
    cv = StratifiedKFold(n_splits=settings.CV_FOLDS, shuffle=True, random_state=settings.RANDOM_STATE)
    cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="roc_auc")
    cv_mean = float(cv_scores.mean()) if not np.isinf(cv_scores.mean()) else 0.0
    cv_std = float(cv_scores.std()) if not np.isinf(cv_scores.std()) else 0.0

    return {
        # ── 主指标：决策阈值下（对外报的就是这一组）──
        "accuracy": round(float(accuracy_score(y_test, y_pred)), 4),
        "precision": round(float(precision_score(y_test, y_pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, y_pred, zero_division=0)), 4),
        "f1_score": round(float(f1_score(y_test, y_pred, zero_division=0)), 4),
        "decision_threshold": round(float(decision_thr), 4),
        # ── 对照指标：默认 0.5 阈值下（保留以便追溯，不作为对外口径）──
        "at_default": {
            "threshold": 0.5,
            "accuracy": round(float(accuracy_score(y_test, y_pred_def)), 4),
            "precision": round(float(precision_score(y_test, y_pred_def, zero_division=0)), 4),
            "recall": round(float(recall_score(y_test, y_pred_def, zero_division=0)), 4),
            "f1_score": round(float(f1_score(y_test, y_pred_def, zero_division=0)), 4),
        },
        "auc": round(float(roc_auc_score(y_test, y_proba)), 4),
        "cv_auc_mean": round(cv_mean, 4),
        "cv_auc_std": round(cv_std, 4),
        "roc_curve": roc_data,
        "confusion_matrix": confusion_data,
        "feature_importance": feature_importance,
    }


def _atomic_write_bytes(target: Path, write_fn) -> None:
    """把 write_fn(tmp_path) 写到同目录临时文件，再原子替换到 target。

    ⚠ 为什么必须这样写（这是「模型重训后页面整段失效」的根因）：
      旧实现是 `open(path, 'w')` + `json.dump(...)` / `joblib.dump(model, path)`，
      而 `open(path, 'w')` 会**先把文件截断为 0 字节**，再逐块写内容。
      写入期间（meta.json 590 KB、random_forest.joblib 实测 180 MB，
      后者在绑定挂载上要数秒），任何读取方看到的都是 0 字节或半截 JSON。

      读取方 model_service._ensure_meta() 是 `json.load()` 失败即 return False，
      于是 /model/comparison、/roc-curves、/feature-importance、/shap-global
      会**静默返回** {"error": "模型尚未训练"} 且 HTTP 200（日志里连 500 都没有）；
      cost_benefit._load_best_model() 返回 None，导致 /portfolio/matrix、
      /cost-benefit/* 同样退化。前端把这些当成正常响应渲染，于是只剩空框。

      实测（同尺寸 533 KB JSON，一边重写一边读）：
          旧实现 open(w)+json.dump  → 读取 172 次，空文件 48 + 解析失败 49 = 56.4% 失败
          临时文件 + os.replace     → 读取 170 次，0 失败

      os.replace 在同一文件系统内是原子的：读取方要么看到旧的完整文件，
      要么看到新的完整文件，**不存在中间态**。
    """
    tmp = target.with_name(target.name + ".tmp")
    write_fn(tmp)
    os.replace(tmp, target)   # 同目录内原子替换


def _save_results_to_disk(models: dict, results: dict, shap_results: dict = None):
    """将模型文件、元数据、SHAP 结果持久化到磁盘（全部原子写入）。

    写入顺序有意为之：
        1. 先把**所有**模型写成 .tmp（此时对读取方完全不可见）；
        2. 逐个 os.replace 模型文件；
        3. **最后**替换 meta.json。

    因为读取方的入口是 meta.json（先读它选出 best model，再加载对应文件），
    把 meta.json 放在最后意味着：只有全部模型都就位后，新的 meta 才会出现。
    残留的窗口是「新 meta 尚未发布、但模型文件已是新版本」，此时旧 meta 仍指向
    可以正常加载并预测的模型，只是版本略新 —— 不会崩、不会空框。
    """
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1) 全部先写临时文件 ──────────────────────────────
    pending: list[tuple[Path, Path]] = []   # (tmp, final)
    for name, model in models.items():
        filename = name.lower().replace(" ", "_") + ".joblib"
        final = MODEL_DIR / filename
        tmp = final.with_name(final.name + ".tmp")
        joblib.dump(model, tmp)
        pending.append((tmp, final))

    meta = {
        "results": results,
        "feature_names": FEATURE_NAMES,
        "shap_global": shap_results or {},
    }
    meta_final = MODEL_DIR / "meta.json"
    meta_tmp = meta_final.with_name(meta_final.name + ".tmp")
    with open(meta_tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)

    # ── 2) 原子替换模型文件 ──────────────────────────────
    for tmp, final in pending:
        os.replace(tmp, final)

    # ── 3) meta.json 最后发布（读取方的"提交点"）─────────
    os.replace(meta_tmp, meta_final)


def _compute_shap(models: dict, X_test, y_test) -> dict:
    """计算所有模型的全局 SHAP 重要性（使用抽样）。"""
    import shap
    shap_results = {}

    # 抽样 200 条做 SHAP（全量太慢）
    n_sample = min(200, len(X_test))
    X_sample = X_test[:n_sample]

    for name, model in models.items():
        try:
            if hasattr(model, "feature_importances_"):
                explainer = shap.TreeExplainer(model)
            else:
                explainer = shap.KernelExplainer(
                    model.predict_proba,
                    shap.sample(X_test, min(100, len(X_test))),
                )
            shap_values = explainer.shap_values(X_sample)

            if isinstance(shap_values, list):
                shap_values = shap_values[1]

            mean_abs_shap = np.abs(shap_values).mean(axis=0)
            shap_results[name] = dict(
                zip(FEATURE_NAMES, [round(float(v), 6) for v in mean_abs_shap])
            )
        except Exception as e:
            shap_results[name] = {"error": str(e)}

    return shap_results


def _save_results_to_db(db, results: dict):
    """将评估指标写入数据库。"""
    for name, result in results.items():
        existing = db.query(ModelResult).filter_by(model_name=name).first()
        if existing:
            existing.auc = result["auc"]
            existing.accuracy = result["accuracy"]
            existing.precision_score = result["precision"]
            existing.recall = result["recall"]
            existing.f1_score = result["f1_score"]
            existing.roc_curve_data = json.dumps(result["roc_curve"])
            existing.feature_importance = json.dumps(result["feature_importance"])
        else:
            db.add(ModelResult(
                model_name=name,
                auc=result["auc"],
                accuracy=result["accuracy"],
                precision_score=result["precision"],
                recall=result["recall"],
                f1_score=result["f1_score"],
                training_time=0,
                roc_curve_data=json.dumps(result["roc_curve"]),
                feature_importance=json.dumps(result["feature_importance"]),
            ))
    db.commit()


@celery_app.task(bind=True, name="train_all_models")
def train_all_models_task(self) -> dict:
    """Celery 任务: 训练全部模型并持久化。

    Returns:
        {"status": "completed", "results": {...}, "best_model": "..."}
    """
    db = SessionLocal()
    try:
        self.update_state(state="PROGRESS", meta={"step": "loading", "message": "加载数据..."})

        loader = DataLoader(db)
        df = loader.load_all()
        X, y, feature_names = prepare_features(df)

        self.update_state(state="PROGRESS", meta={"step": "splitting", "message": "划分训练/测试集..."})

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE,
            random_state=settings.RANDOM_STATE, stratify=y,
        )

        models = _get_models()
        results = {}
        total = len(models)

        for i, (name, model) in enumerate(models.items()):
            self.update_state(state="PROGRESS", meta={
                "step": "training",
                "current": i + 1,
                "total": total,
                "message": f"训练 {name} ({i+1}/{total})...",
            })

            results[name] = _train_and_evaluate(model, name, X_train, X_test, y_train, y_test)

        # ── 计算 SHAP（训练后自动执行）────────────────
        self.update_state(state="PROGRESS", meta={
            "step": "shap", "message": "计算 SHAP 特征重要性...",
        })
        shap_results = _compute_shap(models, X_test, y_test)

        self.update_state(state="PROGRESS", meta={"step": "saving", "message": "保存模型和结果..."})

        _save_results_to_disk(models, results, shap_results)
        _save_results_to_db(db, results)

        # ⚠ 让 **backend 进程** 感知到「模型已换新」。
        #
        # 背景：`risk_scoring._best_model_cache` 带 600 秒 TTL，且它跑在
        # backend 进程里，而训练跑在 Celery worker 进程里 —— 两者内存不共享。
        # 训练完成后，已加载过模型的 backend worker 会**继续用旧模型**，
        # 直到 TTL 到期（最长 10 分钟）。期间界面显示的分级、Top 名单、
        # 成本收益都是旧模型的产物，且不同 worker 可能给出不同结果
        # （有的已过期、有的没有）。
        #
        # 这里递增一个磁盘版本号，backend 侧在取模型时比对（见
        # risk_scoring._model_version_changed 的说明），从而立即换用新模型。
        from app.services import risk_scoring
        risk_scoring.bump_model_version()

        best_model = max(results.keys(), key=lambda x: results[x]["auc"])

        return {
            "status": "completed",
            "results": results,
            "best_model": best_model,
            "total_samples": len(df),
        }

    except SoftTimeLimitExceeded:
        return {"status": "timeout", "error": "训练超时（30分钟），请检查数据量"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()
