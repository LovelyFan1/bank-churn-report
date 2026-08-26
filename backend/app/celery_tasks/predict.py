"""批量预测 Celery 任务 — 在独立 Worker 进程中执行。

包含:
- batch_score_task: 全量客户打分，返回 Top N + 风险分布
- predict_single_task: 单客户预测（轻量但不阻塞 API）
"""

import numpy as np
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import update

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models.customer import Customer
from app.services.data_loader import DataLoader, prepare_features, GEOGRAPHY_MAP, GENDER_MAP, FEATURE_NAMES
from app.config import settings
from app.services import risk_scoring

import joblib
from pathlib import Path
import json

MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"


def _load_best_model():
    """从磁盘加载 AUC 最高的模型。"""
    meta_path = MODEL_DIR / "meta.json"
    if not meta_path.exists():
        return None, None

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    best_name = max(meta["results"].keys(), key=lambda x: meta["results"][x]["auc"])
    model_path = MODEL_DIR / (best_name.lower().replace(" ", "_") + ".joblib")

    if not model_path.exists():
        return None, None

    return joblib.load(model_path), best_name


FACTOR_STRATEGY_MAP = [
    (["投诉记录", "投诉"], "专属客户经理一对一挽留"),
    (["产品数", "产品超载", "产品"], "产品整合推荐"),
    (["余额", "余额为零"], "专属费率激活"),
    (["活跃", "活跃状态", "非活跃"], "主动外呼关怀"),
    (["满意度"], "满意度回访"),
    (["信用"], "定制化产品优惠方案"),
    (["在网时长"], "VIP费率优惠"),
    (["年龄", "高龄"], "定期回访关怀"),
    (["地区", "德国"], "定制化挽留方案"),
    (["积分"], "积分奖励计划"),
]


def _recommend_strategy(risk_factors: list) -> str:
    """根据风险因素推荐最优先的干预策略"""
    if not risk_factors:
        return ""
    for keywords, strategy in FACTOR_STRATEGY_MAP:
        for factor in risk_factors:
            for kw in keywords:
                if kw in factor:
                    return strategy
    return "满意度回访"  # 默认


SHAP_FEATURE_LABELS = {
    "satisfaction_score": "满意度评分",
    "num_products": "持有产品数",
    "age": "年龄",
    "balance": "账户余额",
    "is_active_member": "活跃状态",
    "credit_score": "信用评分",
    "tenure": "在网时长",
    "estimated_salary": "预估薪资",
    "has_credit_card": "持有信用卡",
    "points_earned": "积分",
    "geography": "地区",
    "gender": "性别",
    "complain": "投诉记录",
}


def _risk_level(prob: float) -> str:
    if prob >= 0.7:
        return "CRITICAL"
    elif prob >= 0.3:
        return "HIGH"
    elif prob >= 0.1:
        return "MEDIUM"
    return "LOW"


def _risk_factors(c: Customer) -> list:
    """从 Customer ORM 对象提取人话风险因素标签"""
    factors = []
    if c.complain == 1:
        factors.append("有投诉记录")
    if c.is_active_member == 0:
        factors.append("非活跃用户")
    if c.num_products >= 3:
        factors.append(f"持有 {c.num_products} 个产品（产品超载）")
    if c.age >= 50:
        factors.append(f"高龄客户（{c.age} 岁）")
    if c.balance == 0:
        factors.append("账户余额为零")
    if c.geography == "Germany":
        factors.append("德国地区客户")
    if c.satisfaction_score <= 2:
        factors.append(f"满意度偏低（{c.satisfaction_score}/5）")
    if c.credit_score < 600:
        factors.append(f"信用评分偏低（{c.credit_score} 分）")
    if c.tenure is not None and c.tenure <= 2:
        factors.append(f"在网时长较短（{c.tenure} 年）")
    return factors


@celery_app.task(bind=True, name="batch_score_all")
def batch_score_task(self, top_n: int = 100) -> dict:
    """Celery 任务: 全量批量打分，分块处理避免 OOM。

    Args:
        top_n: 返回 Top N 高风险客户

    Returns:
        {status, total, top_customers, risk_distribution, model_used}
    """
    db = SessionLocal()
    try:
        self.update_state(state="PROGRESS", meta={"step": "loading", "message": "加载最佳模型..."})

        model, best_name = _load_best_model()
        if model is None:
            return {"status": "failed", "error": "模型尚未训练，请先调用 train 接口"}

        loader = DataLoader(db)
        total = loader.total_count

        self.update_state(state="PROGRESS", meta={
            "step": "scoring",
            "message": f"批量预测中（共 {total} 条）...",
            "total": total,
            "processed": 0,
        })

        # 分块处理，写入 DB
        risk_dist = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        top_records = []  # [(prob, customer_dict), ...]

        processed = 0
        for chunk in loader.iter_chunks():
            X, _, _ = prepare_features(chunk)
            raw_probs = model.predict_proba(X)[:, 1]
            cal_probs, levels = risk_scoring.calibrate_probs(db, raw_probs)

            for i in range(len(cal_probs)):
                prob = float(cal_probs[i])
                level = levels[i]
                risk_dist[level] += 1

                # 保留 Top N（记录 + 特征向量，稍后用 SHAP 归因）
                row = chunk.iloc[i]
                record = {
                    "id": int(row["id"]),
                    "customer_id": str(row["customer_id"]),
                    "surname": str(row["surname"]),
                    "geography": str(row["geography"]),
                    "age": int(row["age"]),
                    "balance": round(float(row["balance"]), 2),
                    "estimated_salary": round(float(row["estimated_salary"]), 2),
                    "num_products": int(row["num_products"]),
                    "is_active_member": int(row["is_active_member"]),
                    "complain": int(row["complain"]),
                    "probability": round(prob, 4),
                    "risk_level": level,
                }
                top_records.append((prob, record, X[i]))

            processed += len(chunk)
            self.update_state(state="PROGRESS", meta={
                "step": "scoring",
                "message": f"批量预测中... {processed}/{total}",
                "total": total,
                "processed": processed,
            })

        # Top N 排序
        top_records.sort(key=lambda x: x[0], reverse=True)
        top_n_records = top_records[:top_n]

        # ── SHAP 归因：为 Top N 客户生成带贡献度的风险因素 ──
        self.update_state(state="PROGRESS", meta={"step": "shap", "message": "SHAP 归因分析中..."})
        _enrich_with_shap(model, top_n_records)

        top_customers = [r[1] for r in top_n_records]

        return {
            "status": "completed",
            "total_customers": total,
            "top_customers": top_customers,
            "risk_distribution": risk_dist,
            "model_used": best_name,
        }

    except SoftTimeLimitExceeded:
        return {"status": "timeout", "error": "批量预测超时"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()


def _enrich_with_shap(model, top_records: list) -> None:
    """用 SHAP 值为 Top N 客户的 record 填充 risk_factors（带贡献百分比）。

    Args:
        model: 已加载的最佳模型
        top_records: [(prob, record, feature_vector), ...] — 原地修改 record["risk_factors"]
    """
    if not top_records:
        return

    try:
        import shap

        # TreeExplainer 对树模型最轻量
        explainer = shap.TreeExplainer(model, model_output="probability")
        features_matrix = np.array([r[2] for r in top_records])
        shap_values = explainer.shap_values(features_matrix)

        if isinstance(shap_values, list):
            shap_values = shap_values[1]  # 二分类取正类

        for idx, (prob, record, _) in enumerate(top_records):
            shap_vals = shap_values[idx]
            # feature → SHAP contribution
            pairs = [(FEATURE_NAMES[i], float(shap_vals[i]))
                     for i in range(len(FEATURE_NAMES))]
            pairs.sort(key=lambda x: abs(x[1]), reverse=True)

            record["risk_factors"] = [
                f"{SHAP_FEATURE_LABELS.get(f, f)} ({'+' if v > 0 else ''}{v * 100:.1f}%)"
                for f, v in pairs[:6]
                if abs(v) > 0.003  # 过滤贡献极小的特征
            ]
            record["strategy"] = _recommend_strategy(record["risk_factors"])

    except Exception:
        # 回退：模型不支持 TreeExplainer（如 LogisticRegression），用硬阈值
        import traceback
        traceback.print_exc()
        for prob, record, _ in top_records:
            record["risk_factors"] = _risk_factors_from_row(record)
            record["strategy"] = _recommend_strategy(record["risk_factors"])


def _risk_factors_from_row(row) -> list:
    """从 DataFrame 行提取人话风险因素标签（与 _risk_factors 保持同步）"""
    factors = []
    if row.get("complain") == 1:
        factors.append("有投诉记录")
    if row.get("is_active_member") == 0:
        factors.append("非活跃用户")
    if row.get("num_products", 0) >= 3:
        factors.append(f"持有 {int(row['num_products'])} 个产品（产品超载）")
    if row.get("age", 0) >= 50:
        factors.append(f"高龄客户（{int(row['age'])} 岁）")
    if row.get("balance", 0) == 0:
        factors.append("账户余额为零")
    if row.get("geography") == "Germany":
        factors.append("德国地区客户")
    if row.get("satisfaction_score", 5) <= 2:
        factors.append(f"满意度偏低（{int(row['satisfaction_score'])}/5）")
    if row.get("credit_score", 850) < 600:
        factors.append(f"信用评分偏低（{int(row['credit_score'])} 分）")
    if row.get("tenure", 99) <= 2:
        factors.append(f"在网时长较短（{int(row['tenure'])} 年）")
    return factors


# ── 单条预测（同步，但走独立函数方便复用）───────────────

def predict_single_sync(customer_data: dict) -> dict:
    """单客户预测 — 同步版本，供 API 直接调用（轻量，不需要走 Celery）。"""
    model, best_name = _load_best_model()
    if model is None:
        return {"error": "模型尚未训练，请先调用 train 接口"}

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

    prediction = int(model.predict(features)[0])
    probability = float(model.predict_proba(features)[0][1])
    level = _risk_level(probability)

    risk_color = {"CRITICAL": "#ff4444", "HIGH": "#ffaa00", "MEDIUM": "#ffdd00", "LOW": "#44ff44"}[level]

    return {
        "prediction": prediction,
        "probability": round(probability, 4),
        "risk_level": level,
        "risk_color": risk_color,
        "model_used": best_name,
    }
