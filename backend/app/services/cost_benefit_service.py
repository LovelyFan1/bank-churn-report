import numpy as np
from sqlalchemy.orm import Session
from typing import Dict, Any
from app.services.model_service import get_model_service


class CostBenefitService:
    """成本收益分析服务"""

    def __init__(self, db: Session):
        self.db = db

    def analyze_thresholds(self, cost_ratio: float = 5.0) -> Dict[str, Any]:
        """
        分析不同阈值下的成本收益

        cost_ratio: 漏检成本/误报成本 的比值
                     例如 cost_ratio=5 表示漏掉一个流失客户的成本是误报的5倍
        """
        model_service = get_model_service(self.db)
        if not model_service._results:
            model_service.train_all_models()

        # 获取最佳模型的预测概率
        from sklearn.model_selection import train_test_split
        from app.config import settings

        df = model_service._get_dataframe()
        X, y, feature_names = model_service._prepare_features(df)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=settings.TEST_SIZE, random_state=settings.RANDOM_STATE, stratify=y
        )

        best_model_name = max(model_service._results.keys(), key=lambda x: model_service._results[x]["auc"])
        model = model_service._models[best_model_name]
        y_proba = model.predict_proba(X_test)[:, 1]

        thresholds = np.arange(0.05, 0.96, 0.05)
        results = []

        for threshold in thresholds:
            y_pred = (y_proba >= threshold).astype(int)

            # 混淆矩阵元素
            tp = int(np.sum((y_pred == 1) & (y_test == 1)))  # 真阳性：正确识别流失
            fp = int(np.sum((y_pred == 1) & (y_test == 0)))  # 误报：误判为流失
            fn = int(np.sum((y_pred == 0) & (y_test == 1)))  # 漏检：遗漏了流失客户
            tn = int(np.sum((y_pred == 0) & (y_test == 0)))  # 真阴性：正确识别留存

            # 成本收益计算（单位：万元）
            # 假设：挽留一个客户收益 = 1单位，漏检一个流失客户成本 = cost_ratio单位，误报成本 = 1单位
            benefit = tp * 1  # 挽留成功的收益
            cost_fn = fn * cost_ratio  # 漏检成本
            cost_fp = fp * 1  # 误报成本
            net_profit = benefit - cost_fn - cost_fp

            # 关键指标
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
                "benefit": round(float(benefit), 2),
                "cost_fn": round(float(cost_fn), 2),
                "cost_fp": round(float(cost_fp), 2),
            })

        # 找最优阈值（按净利润排序）
        optimal = max(results, key=lambda x: x["net_profit"])

        return {
            "cost_ratio": cost_ratio,
            "best_model": best_model_name,
            "test_size": len(y_test),
            "churn_count": int(y_test.sum()),
            "thresholds": results,
            "optimal_threshold": optimal["threshold"],
            "optimal_metrics": optimal,
        }

    def get_business_summary(self) -> Dict[str, Any]:
        """获取业务摘要 - 用于前端展示"""
        analysis = self.analyze_thresholds(cost_ratio=5.0)
        opt = analysis["optimal_metrics"]

        # 模拟年化数据
        total_customers = 10000
        annual_churn_rate = 0.2037
        avg_customer_value = 50000  # 假设客户年均价值5万

        annual_churn_count = int(total_customers * annual_churn_rate)
        annual_loss = annual_churn_count * avg_customer_value

        # 使用模型后的预期改善
        retained_with_model = int(annual_churn_count * opt["recall"])
        reduced_loss = retained_with_model * avg_customer_value

        return {
            "total_customers": total_customers,
            "annual_churn_count": annual_churn_count,
            "annual_churn_rate": round(annual_churn_rate * 100, 2),
            "avg_customer_value": avg_customer_value,
            "annual_loss": annual_loss,
            "optimal_threshold": opt["threshold"],
            "model_recall": opt["recall"],
            "retained_customers": retained_with_model,
            "reduced_loss": reduced_loss,
            "roi": round(reduced_loss / (annual_loss * 0.1), 2) if annual_loss > 0 else 0,
        }


def get_cost_benefit_service(db: Session) -> CostBenefitService:
    return CostBenefitService(db)
