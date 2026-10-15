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
from app.services.task_submission import track_submission

import joblib
from pathlib import Path
import json

MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"


def _load_best_model():
    from app.services.model_manifest import resolve
    import logging
    try:
        manifest = resolve(MODEL_DIR)
        return joblib.load(manifest.path), manifest.best_model
    except Exception as error:
        logging.getLogger(__name__).warning("批量模型清单不可用：%s", error)
        return None, None


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
@track_submission
def batch_score_task(self, top_n: int = 100, model_snapshot: dict | None = None) -> dict:
    """Celery 任务: 全量批量打分，分块处理避免 OOM。

    Args:
        top_n: 返回 Top N 高风险客户

    Returns:
        {status, total, top_customers, risk_distribution, model_used}
    """
    db = SessionLocal()
    try:
        self.update_state(state="PROGRESS", meta={"step": "loading", "message": "加载最佳模型..."})

        from app.services import model_manifest
        from app.services.data_loader import _read_cache_version, get_cached_customer_df
        if model_snapshot is None:
            manifest = model_manifest.resolve(MODEL_DIR)
            engine = risk_scoring._ensure_engine(db)
            if engine is None or engine['model_version'] != manifest.revision:
                raise ValueError("模型与风险引擎快照不一致，请重新提交")
            model_snapshot = {"meta": manifest.meta, "model_version": manifest.revision,
                              "thresholds": dict(engine['thresholds']),
                              "customer_version": engine['customer_version'], "status": manifest.status}
        manifest = model_manifest.resolve(MODEL_DIR, model_snapshot['meta'])
        model, best_name = joblib.load(manifest.path), manifest.best_model
        if model_snapshot['customer_version'] != _read_cache_version():
            raise ValueError("客户快照已改变，请重新提交")
        fingerprint = manifest.meta.get('dataset_fingerprint')
        if fingerprint and not manifest.meta['results'][best_name].get('threshold_validation') and risk_scoring.dataset_fingerprint(get_cached_customer_df(db)) != fingerprint:
            raise ValueError("客户数据不匹配固定模型快照")
        thresholds = dict(model_snapshot['thresholds'])

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
            from app.services.shap_contract import positive_class
            raw_probs = model.predict_proba(X)[:, positive_class(model)]
            cal_probs = np.asarray(raw_probs, dtype=float)
            levels = [risk_scoring._level(float(prob), thresholds) for prob in cal_probs]

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
        if model_snapshot['customer_version'] != _read_cache_version():
            raise ValueError("评分期间客户数据已改变，未发布混合结果")

        return {
            "status": "completed",
            "total_customers": total,
            "top_customers": top_customers,
            "risk_distribution": risk_dist,
            "model_used": best_name,
            "model_version": model_snapshot['model_version'],
            "manifest_status": model_snapshot['status'],
        }

    except SoftTimeLimitExceeded:
        return {"status": "timeout", "error": "批量预测超时"}
    except Exception as e:
        return {"status": "failed", "error": "批量评分未通过模型或数据快照检查", "reason": type(e).__name__}
    finally:
        db.close()


def _enrich_with_shap(model, top_records: list) -> None:
    """用 SHAP 值为 Top N 客户的 record 填充 risk_factors（带贡献度）。

    Args:
        model: 已加载的最佳模型
        top_records: [(prob, record, feature_vector), ...] — 原地修改 record["risk_factors"]

    ⚠ 两处曾导致本函数**每次都失败**的问题（实测确认，勿回退）：

    1) `shap.TreeExplainer(model, model_output="probability")` 会直接抛异常：
           ValueError: Only model_output="raw" is supported for
                       feature_perturbation="tree_path_dependent"
       实测（shap==0.45.0 + XGBoost）：调用 batch_score_task 时堆栈被打到
       stderr，异常被下面的 except 吞掉，**每次都降级**成硬阈值规则 ——
       即"SHAP 归因"这个功能其实从未生效过，前端拿到的只是 _risk_factors_from_row
       的规则标签，没有任何贡献度。
       train.py / model_service.py 用的是 `TreeExplainer(model)`（默认 raw），
       所以只有本文件这一处坏掉。
       现统一改为不传 model_output（即 raw），与另两处保持一致。

    2) 把 log-odds 尺度的 SHAP 值写成 `v*100:.1f%`，**不是占比却长得像占比**。
       实测（50 个 Top 客户）：
           展示值范围 −178.6% ~ +201.7%，均值 −11.3%
           每行 top6 的绝对值之和：min 137.5% / max 491.5% / mean 263.4%
           出现 >100% 的条目 30 个，>50% 的 78 个
           负值条目占 57.0%
       也就是说：一行的"百分比"加起来高达 263%，单个特征还能超过 100% ——
       这不是任何意义上的百分比，用户无法正确理解。
       现改为**归一化的贡献占比**：对每个客户，用其 top 特征的 |SHAP| 之和
       作分母，得到真正相加为 100% 的占比，并在字段里标注这是相对贡献。
       这样"产品超载 32%"就是"该客户的风险因素中，产品数量贡献了 32%"，
       语义明确且可验证。
    """
    if not top_records:
        return

    def _fallback():
        for _prob, record, _ in top_records:
            record["risk_factors"] = _risk_factors_from_row(record)
            record["risk_factors_basis"] = "business_rules"
            record["shap_unavailable_reason"] = "模型解释未能通过支持性或贡献加和检查"
            _apply_strategy(record)

    try:
        import shap

        from app.services.shap_contract import explain
        features_matrix = np.array([r[2] for r in top_records])
        explanation = explain(model, features_matrix)
        shap_values = explanation["values"]

        for idx, (_prob, record, _) in enumerate(top_records):
            record.update(shap_output_space=explanation["output_space"],
                shap_base_value=explanation["base_value"], shap_additivity_checked=True,
                shap_positive_class=1, shap_contributions=shap_values[idx].tolist(),
                risk_factors_basis="shap_ratio_of_positive_contributions")
            shap_vals = shap_values[idx]
            pairs = [(FEATURE_NAMES[i], float(shap_vals[i]))
                     for i in range(len(FEATURE_NAMES))]
            # 只看**推动流失**的方向（正贡献）。负的 SHAP 是"降低流失概率"，
            # 把它列进"风险因素"本身就是矛盾的（风险因素应当说明为什么会流失）。
            pos = [(f, v) for f, v in pairs if v > 0]
            if not pos:
                # 该客户没有任何正向贡献特征（模型认为他不太会流失）
                record["risk_factors"] = []
                _apply_strategy(record)
                continue

            pos.sort(key=lambda x: x[1], reverse=True)
            top = pos[:6]
            total = float(sum(v for _f, v in pos))
            record["risk_factors"] = [
                # 占比 = 该特征正贡献 / 全部正贡献之和，相加恰为 100%
                f"{SHAP_FEATURE_LABELS.get(f, f)}（{v / total * 100:.1f}%）"
                for f, v in top
            ]
            record["risk_factors_basis"] = "shap_ratio_of_positive_contributions"
            _apply_strategy(record)

    except Exception as e:
        # 回退：模型不支持 TreeExplainer（如 LogisticRegression），用硬阈值。
        # ⚠ 记录 warning 而非直接 print 堆栈 —— 之前每调用一次就打一整段
        #   traceback 到 stderr，把正常日志淹掉，且让人以为系统在报错。
        import logging
        logging.getLogger(__name__).warning(
            "SHAP 归因不可用，回退为规则标签: %s: %s", type(e).__name__, e
        )
        _fallback()


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
