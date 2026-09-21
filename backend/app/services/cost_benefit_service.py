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
        """从磁盘加载最佳模型, 返回 (model, model_name, meta)。

        ⚠ 两处修复：
        1) **不再每次请求都重新加载**。此前本方法没有缓存，而实例由
           `get_cost_benefit_service(db)` 每请求新建，所以每个请求都要
           重新读 meta.json 并 joblib.load 模型。干预策略页一次并发 5 个请求
           （summary/thresholds/retention/matrix/risk-info），其中 2 个走这里，
           等于每次翻页都把模型反序列化两遍。
        2) **读取失败降级而非抛异常**。训练任务写文件期间（旧实现非原子），
           json.load / joblib.load 会抛 —— 上层接口直接 500 或静默空数据。
           现在统一返回 (None, None, None)，由调用方给出"模型未就绪"。
        """
        meta = self._read_meta()
        if meta is None:
            return None, None, None

        try:
            best_name = max(meta["results"].keys(), key=lambda x: meta["results"][x]["auc"])
        except (KeyError, ValueError):
            return None, None, None

        # 复用 risk_scoring 的进程级模型缓存（避免同一进程内重复反序列化）
        cached_name = risk_scoring._best_model_cache["name"]
        cached_model = risk_scoring._best_model_cache["model"]
        if cached_model is not None and cached_name == best_name \
                and not risk_scoring._cache_expired():
            return cached_model, best_name, meta

        model_path = MODEL_DIR / (best_name.lower().replace(" ", "_") + ".joblib")
        if not model_path.exists():
            return None, None, None

        try:
            model = joblib.load(model_path)
        except Exception as e:
            logger.warning("cost_benefit: 加载模型 %s 失败: %s: %s",
                           model_path, type(e).__name__, e)
            # 退回 risk_scoring 缓存（可能已持有可用模型），避免整页失效
            if cached_model is not None:
                return cached_model, cached_name, meta
            return None, None, None

        return model, best_name, meta

    def _read_meta(self):
        """读取 meta.json，失败时短重试并返回 None（不抛异常）。"""
        meta_path = MODEL_DIR / "meta.json"
        if not meta_path.exists():
            return None
        last_err = None
        for attempt in range(3):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError, OSError) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(0.15 * (attempt + 1))
        logger.warning("cost_benefit: 读取 %s 失败（已重试）: %s: %s",
                       meta_path, type(last_err).__name__, last_err)
        return None

    def analyze_thresholds(self, cost_ratio: float | None = None) -> Dict[str, Any]:
        """分析不同阈值下的成本收益。

        cost_ratio: 漏检成本/误报成本 的比值
                    例如 cost_ratio=5 表示漏掉一个流失客户的成本是误报的 5 倍

        成本模型统一由 risk_scoring.net_profit() 提供（全系统唯一一份），
        本方法只负责遍历阈值、统计混淆矩阵、做**同一阈值下**的指标对比，
        **不负责挑选「最优阈值」** —— 见下方注释。
        """
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

        for threshold in thresholds:
            y_pred = (y_proba >= threshold).astype(int)

            tp = int(np.sum((y_pred == 1) & (y_test == 1)))
            fp = int(np.sum((y_pred == 1) & (y_test == 0)))
            fn = int(np.sum((y_pred == 0) & (y_test == 1)))
            tn = int(np.sum((y_pred == 0) & (y_test == 0)))

            # ⚠ 三个分项必须与 `net_profit` **同口径**，否则同一响应里
            #   会出现互相矛盾的数字（实测缺陷：旧实现 19/19 档都对不上）。
            #
            # 旧实现写的是：
            #     benefit = tp * (cost_ratio - 1)
            #     cost_fn = fn * cost_ratio      ← 把"漏检"记了账
            #     cost_fp = fp * 1
            # 而 net_profit = tp*(cost_ratio-1) - fp，**不含 FN 项**。
            # 两套记账法并存，实测 t=0.20 时
            #     benefit - cost_fn - cost_fp = 4090，而 net_profit = 8590，
            #     差额恒为 fn*cost_ratio。
            #
            # 记账基准是「不干预」：漏掉的流失客户在两种情形下都会流失，
            # 因此 FN 不产生**增量**损失（详见 risk_scoring.net_profit 的说明）。
            # 所以 cost_fn 恒为 0，收益与成本只算 TP 与 FP 两项。
            benefit = tp * (cost_ratio - 1)   # TP 净收益：保住的价值 - 干预成本
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
                # FN 的**名义**代价（fn*cost_ratio）。与 net_profit 口径不同
                # （净收益以"不干预"为基准，FN 不产生增量损失），故单独命名并
                # 附口径说明 —— 保留它是为了不丢失"漏检规模"这一信息，
                # 同时避免与 net_profit 混算。
                "fn_notional_cost": round(float(fn * cost_ratio), 2),
                "cost_basis": "baseline_no_intervention",
            })

        optimal = max(results, key=lambda x: x["net_profit"])

        return {
            "cost_ratio": cost_ratio,
            "best_model": best_name,
            "test_size": len(y_test),
            "churn_count": int(y_test.sum()),
            "thresholds": results,
            "optimal_threshold": optimal["threshold"],
            "optimal_metrics": optimal,
        }

    def get_business_summary(self) -> Dict[str, Any]:
        """业务摘要 — 年化成本收益（**推算值**，非实测结果）。

        口径：模型测试集指标 × 假设客单价。ROI 的分母是「完成一次干预的
        人工成本」，不再是此前那个凭空的比例系数（旧代码写的是
        `annual_loss * 0.1`，无法回答 0.1 从哪来）。
        """
        analysis = self.analyze_thresholds()
        if "error" in analysis:
            return analysis

        opt = analysis.get("optimal_metrics", {})

        # 客户总量与流失率取自真实数据，不再写死 10000 / 0.2037
        total_customers = self.db.query(Customer).count() or settings.NUM_CUSTOMERS
        churn_count = self.db.query(Customer).filter(Customer.exited == 1).count()
        annual_churn_rate = (churn_count / total_customers) if total_customers else settings.CHURN_RATE

        avg_customer_value = settings.AVG_CUSTOMER_VALUE
        # 单次干预成本由 cost_ratio 推导（cost_ratio = 漏检成本 / 误报成本），
        # 保持与 analyze_thresholds 的成本模型同一套假设 —— 见 config.py 的推导说明。
        cost_per_intervention = avg_customer_value / settings.COST_RATIO

        annual_churn_count = int(total_customers * annual_churn_rate)
        annual_loss = annual_churn_count * avg_customer_value

        recall = opt.get("recall", 0)
        precision = opt.get("precision", 0)

        # 触达人次 = 预测为正的客户数（测试集上为 TP+FP，按比例外推到全年）
        annual_flagged = annual_churn_count * (recall / precision) if precision > 0 else 0

        retained_with_model = int(annual_churn_count * recall)
        reduced_loss = retained_with_model * avg_customer_value

        # 真实投入 = 触达人次 × 单次干预成本
        intervention_cost = annual_flagged * cost_per_intervention
        roi = (reduced_loss / intervention_cost) if intervention_cost > 0 else 0

        return {
            "total_customers": total_customers,
            "annual_churn_count": annual_churn_count,
            "annual_churn_rate": round(annual_churn_rate * 100, 2),
            "avg_customer_value": avg_customer_value,
            "cost_per_intervention": round(cost_per_intervention, 2),
            "annual_flagged": int(annual_flagged),
            "intervention_cost": round(intervention_cost, 2),
            "annual_loss": annual_loss,
            "optimal_threshold": opt.get("threshold", 0.5),
            "model_recall": recall,
            "model_precision": precision,
            "retained_customers": retained_with_model,
            "reduced_loss": reduced_loss,
            "roi": round(roi, 2),
            "basis": "estimate",  # 标记口径：推算值
            "note": "基于模型测试集指标与假设客单价推算，非实际业务结果",
        }

    # ── 挽留效果复盘（实测值）───────────────────────────

    def get_retention_summary(self) -> Dict[str, Any]:
        """从 work_orders 的真实处理结果聚合挽留效果。

        数据来源是工单表里已经存在的 result 字段（retained / lost）与
        completed_at，属于**实际执行结果**，与 get_business_summary 的
        推算值口径不同。

        工单表为空时返回 has_data=False，前端据此显示空态 ——
        不得用推算值顶替，否则会把「模型理论上能省钱」说成「实际挽回了客户」。
        """
        completed = (
            self.db.query(WorkOrder)
            .filter(WorkOrder.result.in_(["retained", "lost"]))
            .all()
        )

        if not completed:
            return {
                "has_data": False,
                "total_completed": 0,
                "retained": 0,
                "lost": 0,
                "success_rate": 0.0,
                "roi": 0.0,
                "by_strategy": [],
                "by_assignee": [],
                "basis": "actual",
            }

        total = len(completed)
        retained = sum(1 for o in completed if o.result == "retained")
        success_rate = retained / total if total else 0.0

        # 实测投入与收益：复用与推算值同一套成本假设（config 推导而来），
        # 差异只来自「实际挽留了多少人」这一点，两组数字才可比。
        avg_customer_value = settings.AVG_CUSTOMER_VALUE
        cost_per_intervention = avg_customer_value / settings.COST_RATIO

        cost = total * cost_per_intervention
        benefit = retained * avg_customer_value
        roi = (benefit / cost) if cost > 0 else 0.0

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
                }
                for k, v in buckets.items()
            ]
            rows.sort(key=lambda r: r["total"], reverse=True)
            return rows

        return {
            "has_data": True,
            "total_completed": total,
            "retained": retained,
            "lost": total - retained,
            "success_rate": round(success_rate, 4),
            "cost": round(cost, 2),
            "benefit": round(benefit, 2),
            "roi": round(roi, 2),
            "by_strategy": _group(lambda o: o.strategy, "strategy", "未填写策略"),
            "by_assignee": _group(lambda o: o.assignee, "assignee", "未指派"),
            "basis": "actual",  # 标记口径：实测值
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
