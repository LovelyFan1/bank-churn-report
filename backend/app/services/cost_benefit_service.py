"""成本收益分析服务 — 依赖已训练模型（从磁盘加载）。

包含两组**口径不同、不可混用**的指标：
1. `analyze_thresholds` / `get_business_summary` —— 「推算值」：
   模型在测试集上的表现 × 假设客单价，衡量「模型理论上能省多少」。
2. `get_retention_summary` —— 「实测值」：
   从 work_orders 表的真实处理结果聚合，衡量「实际执行后挽留了多少」。
前端必须分别标注来源，不得把推算值当作实测结果展示。
"""

import json
import logging
import time
import numpy as np
from pathlib import Path
from sqlalchemy import func
from sqlalchemy.orm import Session
from typing import Dict, Any

from app.config import settings
from app.models.customer import Customer
from app.models.work_order import WorkOrder
from app.services.data_loader import prepare_features, get_cached_customer_df
from app.services import risk_scoring
from sklearn.model_selection import train_test_split
import joblib

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"

# 全量客户 DataFrame 的来源说明（供本文件内注释引用）：
#   此前 analyze_thresholds / get_segment_matrix 各自 `DataLoader(db).load_all()`，
#   10 万行实测各约 2.8 秒，而这是**同一张表**、同一次读。现统一走
#   data_loader.get_cached_customer_df()，与 EDA、风险引擎共享同一份。
#   ⚠ 该函数返回共享对象，本文件只读不改（已验证：全部为 .values / .corr 等读取操作）。


class CostBenefitService:
    """成本收益分析服务 — 从磁盘加载最佳模型进行分析。"""

    def __init__(self, db: Session):
        self.db = db

    def _load_best_model(self):
        from app.services.model_manifest import resolve
        try:
            manifest = resolve(MODEL_DIR)
            if MODEL_DIR == risk_scoring.MODEL_DIR:
                model, name = risk_scoring._load_best_model()
                if model is None or risk_scoring._best_model_cache['version'] != manifest.revision:
                    return None, None, None
                return model, name, risk_scoring._best_model_cache['meta']
            return joblib.load(manifest.path), manifest.best_model, manifest.meta
        except Exception as error:
            logger.warning('收益分析模型不可用：%s', error)
            return None, None, None

    def _read_meta(self):
        from app.services.model_manifest import resolve
        try:
            return resolve(MODEL_DIR).meta
        except Exception as error:
            logger.warning('收益分析清单不可用：%s', error)
            return None

    def analyze_thresholds(self, cost_ratio: float | None = None) -> Dict[str, Any]:
        """分析不同阈值下的成本收益。

        cost_ratio: 漏检成本/误报成本 的比值
                    例如 cost_ratio=5 表示漏掉一个流失客户的成本是误报的 5 倍

        成本模型统一由 risk_scoring.net_profit() 提供（全系统唯一一份），
        本方法只负责遍历阈值、统计混淆矩阵、做**同一阈值下**的指标对比，
        **不负责挑选「最优阈值」** —— 见下方注释。
        """
        engine_status = risk_scoring._ensure_engine(self.db)
        if engine_status is not None and not engine_status.get('training_dataset_matches', True):
            return {'error': '当前为滚动推理客户集，不能冒充训练时独立测试集；请查看发布评估记录', 'basis':'unavailable_for_current_cohort'}

        cost_ratio = settings.COST_RATIO if cost_ratio is None else cost_ratio
        model, best_name, _ = self._load_best_model()
        if model is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}

        # 加载数据 —— 走共享缓存（与 EDA、风险引擎同一份）
        df = get_cached_customer_df(self.db)
        X, y, feature_names = prepare_features(df)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE,
            random_state=settings.RANDOM_STATE, stratify=y,
        )

        raw_proba = model.predict_proba(X_test)[:, 1]
        y_proba, _ = risk_scoring.calibrate_probs(self.db, raw_proba)

        thresholds = np.arange(0.05, 0.96, 0.05)
        results = []

        # 挽留成功率 —— 全系统唯一来源（config）。本函数与 net_profit 同口径。
        s = risk_scoring.RETENTION_SUCCESS_RATE

        for threshold in thresholds:
            y_pred = (y_proba >= threshold).astype(int)

            tp = int(np.sum((y_pred == 1) & (y_test == 1)))
            fp = int(np.sum((y_pred == 1) & (y_test == 0)))
            fn = int(np.sum((y_pred == 0) & (y_test == 1)))
            tn = int(np.sum((y_pred == 0) & (y_test == 0)))

            # ⚠ 三个分项必须与 `net_profit` **同口径**，否则同一响应里
            #   会出现互相矛盾的数字（实测缺陷：旧实现 19/19 档都对不上）。
            #
            # 记账基准是「不干预」：漏掉的流失客户在两种情形下都会流失，
            # 因此 FN 不产生**增量**损失（详见 risk_scoring.net_profit）。
            #
            # ⚠ TP 的收益必须乘「挽留成功率 s」—— 这是后补的一项。
            #   旧实现写 `benefit = tp * (cost_ratio - 1)`，等价于 s=1.0，
            #   即「判对了就等于留住了」。实测这一步会让 ROI 高估约 3.3 倍
            #   （s=0.30 时）。s 与 net_profit 用同一个来源，保证两处不漂移。
            benefit = tp * (s * cost_ratio - 1)   # TP 期望净收益（含成功率）
            cost_fn = 0                       # 与净收益口径一致：FN 不计增量
            cost_fp = fp * 1                  # 每次白跑一趟 = 1 个干预成本
            net_profit = risk_scoring.net_profit(tp, fp, fn, cost_ratio)
            # 自检：三个分项相减应恰好等于 net_profit。
            # 留这个断言是为了防止将来有人只改一处口径又造成矛盾。
            assert abs((benefit - cost_fn - cost_fp) - net_profit) < 1e-6, (
                f"成本分项与 net_profit 口径不一致: "
                f"{benefit}-{cost_fn}-{cost_fp} != {net_profit}"
            )

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
                # 三个分项满足 benefit - cost_fn - cost_fp == net_profit
                "benefit": round(float(benefit), 2),
                "cost_fn": round(float(cost_fn), 2),
                "cost_fp": round(float(cost_fp), 2),
                # 按 s=1 计的 TP 名义收益，供对照（明确标注，不参与选阈值）
                "benefit_if_success_100pct": round(float(tp * (cost_ratio - 1)), 2),
                # FN 的**名义**代价（fn*cost_ratio）。与 net_profit 口径不同
                # （净收益以"不干预"为基准，FN 不产生增量损失），故单独命名并
                # 附口径说明 —— 保留它是为了不丢失"漏检规模"这一信息，
                # 同时避免与 net_profit 混算。
                "fn_notional_cost": round(float(fn * cost_ratio), 2),
                "cost_basis": "baseline_no_intervention",
            })

        optimal = max(results, key=lambda x: x["net_profit"])

        # ⚠ 上方 optimal 是在**测试集**上挑出来的，仅作参考曲线用，
        #   **不得**作为对外报告的「最优阈值」—— 那是乐观偏差。
        #
        #   实测缺陷（本次修复暴露）：本方法此前直接返回这个测试集最优值，
        #   而 risk_scoring._ensure_engine 是在**训练集**上选阈值，
        #   两处选出的阈值不同（实测 0.65 vs 0.60），于是
        #       /api/cost-benefit/summary  → optimal_threshold 0.65
        #       /api/model/risk-info       → decision_threshold 0.60
        #   同一系统两个「最优阈值」，且 precision/recall 也跟着不一致
        #   （0.8464/0.2326 vs 0.8099/0.2751）——正是本项目反复出现的
        #   「同一件事两个数」缺陷类型。
        #
        #   现统一以 **risk_scoring 引擎的决策阈值**为准（训练集选、且是全系统
        #   贴名单真正用的那一个），并在响应里同时给出该阈值下的测试集指标。
        engine = risk_scoring._ensure_engine(self.db)
        if engine is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}
        dt = float(engine["decision_threshold"])
        dm = risk_scoring.evaluate_at_decision_threshold() or {}

        # 取引擎阈值在结果表里的那一档（网格对齐后必然存在；找不到则现算）
        official = next((r for r in results if abs(r["threshold"] - dt) < 1e-9), None)
        if official is None:
            official = self._metrics_at(y_proba, y_test, dt, cost_ratio, s)

        return {
            "cost_ratio": cost_ratio,
            "success_rate": s,
            "best_model": best_name,
            "test_size": len(y_test),
            "churn_count": int(y_test.sum()),
            "thresholds": results,
            # ── 对外口径：与 /api/model/risk-info 完全一致（同一个引擎阈值）──
            "optimal_threshold": official["threshold"],
            "optimal_metrics": official,
            # 决策阈值及其在测试集上的实测指标（与 risk-info 同源，可直接对账）
            "decision_threshold": round(dt, 4),
            "decision_coverage": round(float(engine["decision_coverage"]), 4),
            "decision_metrics": dm,
            # 保留"测试集自选最优"仅作曲线参考，字段名自带警告
            "test_set_argmax_threshold": optimal["threshold"],
            "test_set_argmax_metrics": optimal,
            "basis": "estimate",
            "note": (
                f"TP 收益按挽留成功率 {s:.0%} 折算（RETENTION_SUCCESS_RATE）；"
                f"若按 100% 计，最优阈值会偏松。"
                f"该成功率是业务假设值，本系统无真实挽留结果数据可校准。"
                f"optimal_threshold 取自风险引擎（训练集选线），"
                f"与 /api/model/risk-info 的 decision_threshold 同源；"
                f"thresholds 曲线中净收益最大的那档是在测试集上取的（乐观），"
                f"见 test_set_argmax_threshold，仅供参考、不作对外口径。"
            ),
        }

    @staticmethod
    def _metrics_at(y_proba, y_test, threshold: float,
                    cost_ratio: float, s: float) -> Dict[str, Any]:
        """在给定阈值上算一组指标 —— 与主循环同一套公式，供网格外的阈值使用。"""
        y_pred = (y_proba >= threshold).astype(int)
        tp = int(np.sum((y_pred == 1) & (y_test == 1)))
        fp = int(np.sum((y_pred == 1) & (y_test == 0)))
        fn = int(np.sum((y_pred == 0) & (y_test == 1)))
        tn = int(np.sum((y_pred == 0) & (y_test == 0)))
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        return {
            "threshold": round(float(threshold), 2),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "net_profit": round(float(risk_scoring.net_profit(tp, fp, fn, cost_ratio)), 2),
            "benefit": round(float(tp * (s * cost_ratio - 1)), 2),
            "cost_fn": 0.0,
            "cost_fp": float(fp),
            "fn_notional_cost": round(float(fn * cost_ratio), 2),
            "cost_basis": "baseline_no_intervention",
        }

    def get_business_summary(self) -> Dict[str, Any]:
        """业务摘要 — 静态样本情景测算（**推算值**，非实测结果）。

        口径：模型测试集指标 × 假设客单价 × **假设挽留成功率**。

        ⚠ 本次修复的两个问题（实测确认）：

        【1】ROI 此前隐含「挽留成功率 100%」
           旧式子展开后是 `ROI = COST_RATIO × precision`，一个不含成功率
           因子的式子 —— 等价于假设「只要打电话客户就留下」。实测按 s=0.30
           计，这会把 ROI 高估约 3.3 倍。现引入 settings.RETENTION_SUCCESS_RATE。

        【2】字段名把「模型判对」说成了「挽留成功」
           `retained_customers` 的实际含义是 **TP**（模型判为流失且确实流失
           的人数），不是"真的留住了这么多客户"。前端据此标注「挽留成功」，
           属于把预测结果说成业务结果。
           现拆成三个语义明确的字段：
               tp_at_threshold            判对人数（TP），供对账
               expected_retained          期望挽留人数 = TP × s  ← 对外口径
               retained_customers         【兼容旧名】值已改为 expected_retained
                                          （即已乘 s），使尚未更新的旧页面
                                          显示的数字自动变正确
        """
        engine_status = risk_scoring._ensure_engine(self.db)
        if engine_status is not None and not engine_status.get('training_dataset_matches', True):
            return {'error': '当前为滚动推理客户集，不能冒充训练时独立测试集；请查看发布评估记录', 'basis':'unavailable_for_current_cohort'}

        analysis = self.analyze_thresholds()
        if "error" in analysis:
            return analysis

        opt = analysis.get("optimal_metrics", {})
        s = analysis.get("success_rate", risk_scoring.RETENTION_SUCCESS_RATE)

        # 客户总量与流失率取自真实数据，不再写死 10000 / 0.2037
        total_customers = self.db.query(Customer).count() or settings.NUM_CUSTOMERS
        churn_count = self.db.query(Customer).filter(Customer.exited == 1).count()
        annual_churn_rate = (churn_count / total_customers) if total_customers else settings.CHURN_RATE

        avg_customer_value = settings.AVG_CUSTOMER_VALUE
        # 单次干预成本由 cost_ratio 推导（cost_ratio = 漏检成本 / 误报成本），
        # 保持与 analyze_thresholds 的成本模型同一套假设 —— 见 config.py 的推导说明。
        cost_per_intervention = avg_customer_value / settings.COST_RATIO

        # ⚠ 分母用「实际流失人数」而非「客户总数 × 流失率」。
        #   旧实现是 int(total * rate)，实测 96,418 × 0.2040 = 19,669，
        #   而真实流失数是 19,666 —— 差 3 人（浮点取整）。两个数都对外暴露过，
        #   无法对账。直接查库更准且更好解释。
        annual_churn_count = churn_count

        annual_loss = annual_churn_count * avg_customer_value

        recall = opt.get("recall", 0)
        precision = opt.get("precision", 0)

        # 触达人次 = 预测为正的客户数（测试集上为 TP+FP，按比例外推到全年）
        # ⚠ 先取整再用于计算，保证 annual_flagged 与 intervention_cost 内部自洽。
        #   实测缺陷：旧实现把未截断的浮点（6679.98）用于算成本，却把 int()
        #   截断后的 6679 对外显示 —— 两者差约 1 人次（≈1 万元），
        #   使用者按显示的触达人次复算成本会得到不同的数。
        annual_flagged = int(annual_churn_count * (recall / precision)) if precision > 0 else 0

        # TP：模型判为流失、且确实流失的人数（不是"挽留成功"）
        tp_at_threshold = int(round(annual_churn_count * recall))
        # 期望挽留人数 = TP × 挽留成功率 —— 这才是"能留住多少人"的口径
        expected_retained = int(round(tp_at_threshold * s))
        expected_reduced_loss = expected_retained * avg_customer_value

        # 真实投入 = 触达人次 × 单次干预成本
        intervention_cost = annual_flagged * cost_per_intervention
        roi = (expected_reduced_loss / intervention_cost) if intervention_cost > 0 else 0

        return {
            "total_customers": total_customers,
            "annual_churn_count": annual_churn_count,
            "annual_churn_rate": round(annual_churn_rate * 100, 2),
            "avg_customer_value": avg_customer_value,
            "cost_per_intervention": round(cost_per_intervention, 2),
            "annual_flagged": annual_flagged,
            "intervention_cost": round(intervention_cost, 2),
            "annual_loss": annual_loss,
            "optimal_threshold": opt.get("threshold", 0.5),
            "model_recall": recall,
            "model_precision": precision,
            # ── 挽留相关三字段（语义严格区分，勿混用）──
            "success_rate": s,                       # 假设的挽留成功率
            "tp_at_threshold": tp_at_threshold,      # 判对人数（TP）
            "expected_retained": expected_retained,  # 期望挽留人数 = TP × s
            "expected_reduced_loss": expected_reduced_loss,
            # ⚠ 兼容字段：旧前端读的是 retained_customers / reduced_loss。
            #   为不改坏已有页面，此处仍给值，但**语义已改为"期望值"**
            #   （即已乘 s），使旧页面显示的数字自动变正确，而不是继续显示 TP。
            #   新代码请用 expected_retained / expected_reduced_loss。
            "retained_customers": expected_retained,
            "reduced_loss": expected_reduced_loss,
            "roi": round(roi, 2),
            "roi_definition": "benefit / cost",
            "net_roi": round(roi - 1, 4) if intervention_cost > 0 else None,
            "analysis_period": "snapshot",
            "basis": "estimate",  # 标记口径：推算值
            "note": (
                "当前为静态样本的情景测算，缺少年度观察窗口；余额不是利润，收益未经因果验证。"
                f"基于模型测试集指标、假设客单价 ¥{avg_customer_value:,.0f}、"
                f"假设挽留成功率 {s:.0%} 推算，非实际业务结果。"
                f"「期望挽留人数」= 判对人数(TP) × 挽留成功率；"
                f"成功率越低，最优阈值越高、名单越窄。"
            ),
        }

    # ── 挽留效果复盘（实测值）───────────────────────────

    def get_retention_summary(self) -> Dict[str, Any]:
        """从 work_orders 的处理结果聚合挽留效果。

        ⚠ 本方法与 get_business_summary 的 ROI **不可直接比较**，原因如下
          （实测确认，这是本次修复的第二个问题）：

          · get_business_summary 的分母 = 模型决策线覆盖的**全部**人群
            （实测 35,690 人）→ 回答"按模型名单全员触达，账算得过来吗"
          · 本方法的分母 = 实际**已建单完成**的工单数（实测 56 条）
            → 回答"我实际做掉的这些单，效果如何"

          两者分母相差 637 倍。实测后果：本方法的 ROI(3.04) 高于推算值(2.15)，
          但这**不是**因为实际做得更好 —— 而是因为分母只统计了实际执行的那
          一小撮（且这撮还是从 expected_value Top200 里挑的，属高价值易挽留
          的偏斜样本）。若按同口径折算，实际 ROI 反而更低。
          故本次给两个 ROI 各自命名并附 `roi_basis`，页面上不得再并列为"ROI"。

        ⚠ 数据来源的另一重限制（必须让使用者知道）：
          work_orders 的初始内容是 seed_work_orders.py 播种的**演示数据**
          （其 result 来自一张硬编码的比例表，非真实客户反馈）。
          因此 `success_rate` 在未接入真实回访结果前**不具备统计意义**，
          它只是演示流程用的占位值。返回值中 basis 标为
          "manual_observations" 以如实反映这一点。

        工单表为空时返回 has_data=False，前端据此显示空态 ——
        不得用推算值顶替，否则会把「模型理论上能省钱」说成「实际挽回了客户」。
        """
        excluded_demo = self.db.query(WorkOrder).filter(WorkOrder.result_source == 'demo').count()
        historical_unknown = self.db.query(WorkOrder).filter((WorkOrder.result_source == 'unknown') | WorkOrder.result_source.is_(None)).count()
        completed = (
            self.db.query(WorkOrder)
            .filter(WorkOrder.result_source == "manual")
            .filter(WorkOrder.result.in_(["retained", "lost"]))
            .filter(WorkOrder.status.in_(["completed", "lost"]))
            .all()
        )

        if not completed:
            return {
                "has_data": False,
                "total_completed": 0,
                "retained": 0,
                "lost": 0,
                "success_rate": 0.0,
                "roi_executed": 0.0,
                "by_strategy": [],
                "by_assignee": [],
                "basis": "manual_observations",
                "excluded_demo": excluded_demo, "historical_unknown": historical_unknown,
                "is_causal_effect": False,
                "roi_basis": "executed_orders_only",
            }

        verified = [o for o in completed if o.verification_state == 'verified']
        costs = [o.actual_cost for o in completed if o.actual_cost is not None]
        benefits = [o.actual_benefit for o in completed if o.actual_benefit is not None]
        total = len(completed)
        retained = sum(1 for o in completed if o.result == "retained")
        success_rate = retained / total if total else 0.0

        # 实测投入与收益：成本假设与推算值同源（config 推导而来）
        avg_customer_value = settings.AVG_CUSTOMER_VALUE
        cost_per_intervention = avg_customer_value / settings.COST_RATIO

        cost = total * cost_per_intervention
        benefit = retained * avg_customer_value
        roi_executed = (benefit / cost) if cost > 0 else 0.0

        # 与推算值同口径的对照值：把"实际成功率"代进模型口径的式子
        #   ROI_plan = COST_RATIO × precision × s
        # 这里用实测 s 替换假设 s，并取当前模型的 precision，
        # 供使用者看出"计划 vs 实际"的差距到底来自哪一项。
        engine = risk_scoring._ensure_engine(self.db)
        precision_now = None
        if engine is not None:
            dm = risk_scoring.evaluate_at_decision_threshold()
            precision_now = dm.get("precision")

        roi_if_same_basis = None
        if precision_now:
            roi_if_same_basis = round(
                settings.COST_RATIO * precision_now * success_rate, 2)

        def _group(key_fn, key_name: str, empty_label: str):
            """按 key_fn 分组统计成功率。

            key_name 是**固定的字段名**（strategy / assignee），不能由 label 动态生成 ——
            前端按 `s.strategy` / `s.assignee` 取值，动态键名会导致取到 undefined。
            """
            buckets: Dict[str, Dict[str, int]] = {}
            for o in completed:
                k = key_fn(o) or empty_label
                b = buckets.setdefault(k, {"total": 0, "retained": 0})
                b["total"] += 1
                if o.result == "retained":
                    b["retained"] += 1
            rows = [
                {
                    key_name: k,
                    "total": v["total"],
                    "retained": v["retained"],
                    "success_rate": round(v["retained"] / v["total"], 4),
                    # 小样本标记：少于 30 条不给结论（与矩阵的 MIN_CELL_SAMPLE 同标准）
                    "sample_sufficient": v["total"] >= 30,
                }
                for k, v in buckets.items()
            ]
            rows.sort(key=lambda r: r["total"], reverse=True)
            return rows

        return {
            "has_data": True,
            "verified_count": len(verified), "unverified_count": total - len(verified),
            "actual_cost": round(sum(costs),2) if len(costs)==total else None,
            "actual_benefit": round(sum(benefits),2) if len(benefits)==total else None,
            "actual_cost_coverage": len(costs), "actual_benefit_coverage": len(benefits),
            "roi_is_scenario": True,
            "total_completed": total,
            "retained": retained,
            "lost": total - retained,
            "success_rate": round(success_rate, 4),
            "cost": round(cost, 2),
            "benefit": round(benefit, 2),
            # ⚠ 改名：这是「已执行工单口径」的 ROI，不是与推算值可比的 ROI
            "roi_executed": round(roi_executed, 2),
            "roi_basis": "executed_orders_only",
            # 与模型口径对齐后的对照值（用实测成功率代入模型式子）
            "roi_if_same_basis_as_plan": roi_if_same_basis,
            "precision_used": precision_now,
            "plan_success_rate": risk_scoring.RETENTION_SUCCESS_RATE,
            "by_strategy": _group(lambda o: o.strategy, "strategy", "未填写策略"),
            "by_assignee": _group(lambda o: o.assignee, "assignee", "未指派"),
            # 如实标注：数据来自工单表，而该表初始内容是播种的演示记录
            "basis": "manual_observations",
                "excluded_demo": excluded_demo, "historical_unknown": historical_unknown,
                "is_causal_effect": False,
            "note": "仅统计人工声明的观察结果，默认排除演示与来源未知的历史工单；复核数量独立列出。ROI为假设客单价和成本的情景折算，不能视为实际利润或干预增量效果。实际费用/收益仅在逐单完整录入时提供合计。",
        }

    # ── 价值层 × 风险等级 矩阵 ─────────────────────────────

    # 每格样本低于此数时不给出统计量。理由：小样本上算出的召回率/流失率
    # 误差极大，看着像真数字反而更具误导性。实测低价值 × CRITICAL 格在
    # 测试集只有 17 人，属于此列。
    MIN_CELL_SAMPLE = 30

    def get_segment_matrix(self) -> Dict[str, Any]:
        """价值层 × 风险等级 的二维矩阵 —— 「概率 → 决策」中间层。

        这是补上此前缺失的那一维：风险等级回答「会不会跑」，价值层回答
        「跑了值多少」，两者交叉才知道该做什么（高价值高危要客户经理上门，
        零余额高危一条 APP 推送即可）。

        口径说明（**与 analyze_thresholds 严格同源**）：
        - 用同一组 train_test_split 划分（同 seed / 同 stratify），保证与
          /cost-benefit/thresholds 的数字能对账；
        - 等级边界取**训练集**上的分位数（P95/P70/P35），不是测试集 ——
          测试集上算分位数属于用未来信息，且与线上打分时的边界不一致；
        - 测试集仅用于统计「该格历史上真实流失了多少」，即描述性统计。
        因此 basis="descriptive"：描述历史分布，不外推未来收益。
        """
        engine_status = risk_scoring._ensure_engine(self.db)
        if engine_status is not None and not engine_status.get('training_dataset_matches', True):
            return {'error':'当前滚动推理集不等于模型测试集，矩阵不可用','basis':'unavailable_for_current_cohort'}

        model, best_name, _ = self._load_best_model()
        if model is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}

        # 加载数据 —— 走共享缓存（与 analyze_thresholds 同一份，不再重复读表）
        df = get_cached_customer_df(self.db)
        X, y, feature_names = prepare_features(df)

        # 与 analyze_thresholds 同一套划分（seed/stratify 一致），否则两边对不上。
        # 对**索引**划分而不是对 X 划分，因为 X 是 ndarray 不带 index，
        # 而矩阵需要拿回每行的 balance（在 df 里）。抽样规则与对 X 划分等价。
        idx = np.arange(len(df))
        idx_train, idx_test, y_train, y_test = train_test_split(
            idx, y, test_size=settings.TEST_SIZE,
            random_state=settings.RANDOM_STATE, stratify=y,
        )

        # ⚠ 等级边界必须与**列表页/详情页/主线判定**用同一套。
        #
        # 旧实现取的是「训练集上的分位数」，而 risk_scoring._ensure_engine
        # 取的是「全量 10 万行上的分位数」。两者数值接近但**不等**，实测：
        #     全量（列表口径）: critical 0.679573, high 0.247546, medium 0.068197
        #     训练集（矩阵口径）: critical 0.679530, high 0.248164, medium 0.068623
        # 导致**全量 10 万人中有 212 人**在两个页面显示不同风险等级，
        # 例如 C056694 p=0.247966 → 列表 HIGH、矩阵 MEDIUM。
        # 两个接口都把 thresholds 对外暴露，数字却对不上，无法对账。
        #
        # 现改为直接复用 risk_scoring 引擎已算好的全量分位数 ——
        # 那才是线上贴标签、排序、筛选真正用的那一套。
        # 这样 `/api/customers` 的 risk.thresholds 与 `/api/portfolio/matrix`
        # 的 thresholds 逐位相同。
        engine = risk_scoring._ensure_engine(self.db)
        if engine is None:
            return {"error": "模型尚未训练，请先调用 POST /api/model/train"}
        thr = dict(engine["thresholds"])
        # 保留变量以满足下方原有引用（划分仍用于描述性统计）
        p_train = model.predict_proba(X[idx_train])[:, 1]

        # 测试集明细：概率 / 余额 / 真实标签
        test_df = df.iloc[idx_test]
        p_test = model.predict_proba(X[idx_test])[:, 1]

        levels = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        tiers = ["HIGH", "LOW", "ZERO"]

        # 召回率在循环外算一次 —— 它对所有格子相同（模型未分格训练），
        # 每格重算既浪费也会让人误以为各格召回率不同。
        recall = self._global_recall(p_test, np.asarray(y_test))

        rows = []
        for tier in tiers:
            for lv in levels:
                rows.append(self._build_cell(
                    tier, lv, test_df, p_test, np.asarray(y_test), thr, recall
                ))

        # 合计行：按等级与按价值层各一组，供前端核对总账
        return {
            "basis": "descriptive",
            "cohort": "held_out_test",
            "model_version": engine.get('model_version'),
            "model": best_name,
            "test_size": len(y_test),
            "thresholds": {k: round(v, 4) for k, v in thr.items()},
            "min_cell_sample": self.MIN_CELL_SAMPLE,
            "recall_used": round(recall, 4),
            "tiers": tiers,
            "levels": levels,
            "cells": rows,
            "tier_totals": self._totals(rows, "tier", tiers),
            "level_totals": self._totals(rows, "level", levels),
            "note": (
                "描述性统计：基于测试集历史分布，不外推未来收益。"
                "「历史流失率」是该格内真实流失比例；"
                "「预估可挽回」= 该格内**会被决策线触达**的流失客户余额合计"
                "（recall_basis=in_cell_at_decision_threshold）；"
                "「recoverable_value_upper_bound」是假设全部触达的上界，仅供对照。"
                "低风险格在决策线下召回率为 0，故可挽回金额为 0 —— 这是预期结果，"
                "不是数据缺失。"
            ),
        }

    def _build_cell(self, tier: str, level: str, test_df, p_test,
                    y_test: np.ndarray, thr: dict, recall: float) -> Dict[str, Any]:
        """构造矩阵中一格。样本不足时只回人数，不编统计量。"""
        tier_mask = np.array([
            risk_scoring.value_tier(b) == tier for b in test_df["balance"].values
        ])
        level_mask = np.array([
            risk_scoring._level(float(p), thr) == level for p in p_test
        ])
        mask = tier_mask & level_mask
        n = int(mask.sum())

        # 渠道与动作直接取自 risk_scoring 的唯一策略来源，
        # 前端不得再本地维护一份（此前前端 ACTION_BY_TIER 与后端 FACTOR_STRATEGY_MAP
        # 是两个说法，同一个客户在列表页和策略页显示不同动作）。
        rec = risk_scoring.recommend_action(tier, level, [])
        cell = {"tier": tier, "level": level, "count": n,
                "channel": rec["channel"],
                "channel_label": risk_scoring.CHANNEL_LABELS[rec["channel"]],
                "action": rec["action"],
                "sample_sufficient": n >= self.MIN_CELL_SAMPLE}

        if n == 0:
            cell.update({"churn_rate": None, "avg_balance": None,
                         "expected_churn": None, "recoverable_value": None})
            return cell

        bal = test_df["balance"].values[mask].astype(float)
        yt = y_test[mask]

        if not cell["sample_sufficient"]:
            # 人数够看，统计量不给 —— 前端显示「样本不足」
            cell.update({"churn_rate": None, "avg_balance": round(float(bal.mean()), 2),
                         "expected_churn": None, "recoverable_value": None})
            return cell

        churn_mask = yt == 1
        churn_count = int(churn_mask.sum())
        churn_rate = churn_count / n

        # ── 可挽回金额：用**格内实际召回率**，不用全局召回率 ──────────
        #
        # ⚠ 实测缺陷（旧实现）：这里用的是全局 recall（0.7796），
        #   但决策线下每格的实际召回率差异极大 —— 实测：
        #       tier   level       n   churn   格内实际召回   旧实现用的全局值
        #       HIGH   CRITICAL  534     460      1.0000        0.7796  (-22%)
        #       HIGH   HIGH     2812    1115      1.0000        0.7796  (-22%)
        #       HIGH   MEDIUM   4406     642      0.3022        0.7796  (+158%)
        #       HIGH   LOW      1743      70      0.0000        0.7796  (+∞)
        #       ZERO   LOW      4737      90      0.0000        0.7796  (+∞)
        #   最典型的：HIGH×LOW 格算出 recoverable_value = 7,305,001 元，
        #   但该格内所有客户 p < 0.0686 < decision_threshold(0.2)，
        #   决策线下**一个都不会被触达**，真实可挽回为 0。
        #   前端 InterventionStrategy 直接把它展示为"可挽回金额"，
        #   会把人力错误地导向根本不会干预的格子。
        #
        # 现按 `decision_threshold` 逐格统计：只有 p >= 阈值的人才会进名单，
        # 因此格内召回率 = 该格流失客户中 p >= 阈值 的比例。
        # 这会天然得到 0（低风险格）或 1（极高格），是**如实**的结果。
        dt = self._decision_threshold()
        recalled_mask = p_test[mask] >= dt
        recalled_churn = int((churn_mask & recalled_mask).sum())
        cell_recall = (recalled_churn / churn_count) if churn_count else 0.0

        churn_balance = float(bal[churn_mask].sum())
        recalled_balance = float(bal[churn_mask & recalled_mask].sum())

        cell.update({
            "churn_rate": round(churn_rate, 4),
            "avg_balance": round(float(bal.mean()), 2),
            # 两套口径都给出，前端可据标题选择；字段名自带口径，不会混用
            "expected_churn": round(recalled_churn, 1),
            "recoverable_value": round(recalled_balance, 2),
            "recall_used": round(cell_recall, 4),
            "recall_basis": "in_cell_at_decision_threshold",
            # 保留"若全部触达"的上界，便于与旧口径对照（明确标注为上界）
            "recoverable_value_upper_bound": round(churn_balance, 2),
            "decision_threshold": round(float(dt), 4),
        })
        return cell

    def _decision_threshold(self) -> float:
        """当前生效的决策阈值（名单准入线）。

        供逐格统计"该格有多少人真的会被触达"用 —— 见 _build_cell 里
        关于「格内实际召回率 vs 全局召回率」的说明。
        取不到时退回 0.5（与 _global_recall 的兜底一致）。
        """
        engine = risk_scoring._ensure_engine(self.db)
        if not engine:
            return 0.5
        return float(engine.get("decision_threshold")
                     or engine.get("optimal_threshold") or 0.5)

    def _global_recall(self, p_test, y_test) -> float:
        """全局运行阈值下的召回率。阈值取 risk-info 的 optimal_threshold 口径。"""
        thr_run = self._decision_threshold()
        pred = (p_test >= thr_run).astype(int)
        tp = int(np.sum((pred == 1) & (y_test == 1)))
        fn = int(np.sum((pred == 0) & (y_test == 1)))
        return tp / (tp + fn) if (tp + fn) > 0 else 0.0

    @staticmethod
    def _totals(rows: list, key: str, keys: list) -> list:
        """按 key 汇总人数，供前端核对总账（各小计应等于测试集总数）。"""
        return [
            {"key": k, "count": sum(r["count"] for r in rows if r[key] == k)}
            for k in keys
        ]


def get_cost_benefit_service(db: Session) -> CostBenefitService:
    return CostBenefitService(db)
