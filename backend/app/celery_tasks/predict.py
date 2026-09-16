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
                bal = round(float(row["balance"]), 2)
                record = {
                    "id": int(row["id"]),
                    "customer_id": str(row["customer_id"]),
                    "surname": str(row["surname"]),
                    "geography": str(row["geography"]),
                    "age": int(row["age"]),
                    "balance": bal,
                    "estimated_salary": round(float(row["estimated_salary"]), 2),
                    "num_products": int(row["num_products"]),
                    "is_active_member": int(row["is_active_member"]),
                    "complain": int(row["complain"]),
                    "probability": round(prob, 4),
                    "risk_level": level,
                    # 期望价值维度 —— 见下方排序说明
                    "value_tier": risk_scoring.value_tier(bal),
                    "expected_value": risk_scoring.expected_value(prob, bal),
                }
                top_records.append((prob, record, X[i]))

            processed += len(chunk)
            self.update_state(state="PROGRESS", meta={
                "step": "scoring",
                "message": f"批量预测中... {processed}/{total}",
                "total": total,
                "processed": processed,
            })

        # ── Top N 排序：按期望价值 ──
        # 本任务已对**全量**客户打分（上面的循环遍历了每一行），候选集就是全体，
        # 因此无需截断。实测：任何按概率预先截断的池子都会漏掉真正的 EV 头部 ——
        # EV Top10 的概率在 0.97~0.99，在概率榜上排在 100~700 名，
        # 3 倍池命中 0/10，50 倍池才 6/10，只有全量池是 10/10。
        #
        # 排序键用期望价值而非概率：概率在 0.97 以上已无区分度（500 个 CRITICAL
        # 客户全挤在 1.0 附近），此时余额是唯一能拉开差距的维度。实测按概率取
        # Top10 有 4 人余额为 0（打电话也无资产可留），按 EV 取则余额合计为前者
        # 的 3.35 倍。
        top_records.sort(key=lambda x: x[1]["expected_value"], reverse=True)
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
            _apply_strategy(record)

    except Exception:
        # 回退：模型不支持 TreeExplainer（如 LogisticRegression），用硬阈值
        import traceback
        traceback.print_exc()
        for prob, record, _ in top_records:
            record["risk_factors"] = _risk_factors_from_row(record)
            _apply_strategy(record)


def _apply_strategy(record: dict) -> None:
    """就地写入策略三字段 —— 渠道/动作/理由，全部来自 risk_scoring 的唯一来源。

    必须在 record 已带 value_tier 与 risk_level 之后调用。
    """
    rec = risk_scoring.recommend_action(
        record.get("value_tier"), record.get("risk_level"), record.get("risk_factors")
    )
    record["channel"] = rec["channel"]
    # strategy 保留旧名，语义等同 action；建单弹窗仍读它
    record["strategy"] = rec["action"]
    record["action"] = rec["action"]
    record["reason"] = rec["reason"]


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
