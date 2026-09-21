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
    """从磁盘加载 AUC 最高的模型。失败时返回 (None, None)，不抛异常。

    ⚠ 旧实现是裸的 json.load + joblib.load，**没有任何异常处理**。
    实测：把 meta.json 截断成一半（模拟训练任务正在写入的中间状态），
        predict.py 版   → 抛 JSONDecodeError（任务整体失败）
        risk_scoring 版 → 返回 (None, None)，降级为"模型未就绪"并记 warning
    同一份文件、两种处理，说明这里漏了防护。
    train.py 用原子写（tempfile + os.replace）后撞上的概率已很低，但
    "读的时候文件恰好不存在/损坏"仍会发生（例如首次部署、误删、磁盘写满），
    本函数是任务入口，不该因此把整个 batch_score 打成 failed。
    现与 risk_scoring._load_best_model 对齐：重试 + 降级 + warning。
    """
    import logging
    import time as _time
    logger = logging.getLogger(__name__)

    meta_path = MODEL_DIR / "meta.json"
    if not meta_path.exists():
        return None, None

    ATTEMPTS = 3
    meta = None
    last_err: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            break
        except (json.JSONDecodeError, ValueError, OSError) as e:
            last_err = e
            if attempt < ATTEMPTS - 1:
                _time.sleep(0.15 * (attempt + 1))

    if meta is None:
        logger.warning("predict: 读取 %s 失败（已重试 %d 次）: %s: %s",
                       meta_path, ATTEMPTS, type(last_err).__name__, last_err)
        return None, None

    try:
        results = meta["results"]
        best_name = max(results.keys(), key=lambda x: results[x]["auc"])
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("predict: meta.json 结构异常，无法选出最佳模型: %s: %s",
                       type(e).__name__, e)
        return None, None

    model_path = MODEL_DIR / (best_name.lower().replace(" ", "_") + ".joblib")
    if not model_path.exists():
        logger.warning("predict: 最佳模型文件不存在: %s", model_path)
        return None, None

    try:
        return joblib.load(model_path), best_name
    except Exception as e:
        logger.warning("predict: 加载模型 %s 失败: %s: %s",
                       model_path, type(e).__name__, e)
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
            _apply_strategy(record)

    try:
        import shap

        # ⚠ 不传 model_output —— 默认 raw 是唯一被 tree_path_dependent 支持的尺度
        explainer = shap.TreeExplainer(model)
        features_matrix = np.array([r[2] for r in top_records])
        shap_values = explainer.shap_values(features_matrix)

        if isinstance(shap_values, list):
            shap_values = shap_values[1]  # 二分类取正类

        shap_values = np.asarray(shap_values)
        if shap_values.ndim != 2 or shap_values.shape[1] != len(FEATURE_NAMES):
            # 形状不符 → 宁可回退，也不能让索引悄悄错位
            raise ValueError(
                f"SHAP 输出形状 {shap_values.shape} 与 FEATURE_NAMES"
                f"({len(FEATURE_NAMES)}) 不匹配"
            )

        for idx, (_prob, record, _) in enumerate(top_records):
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
