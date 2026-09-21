import pandas as pd
import numpy as np
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.services.data_loader import get_cached_customer_df
from typing import Dict, List, Any


class EDAService:
    """探索性数据分析服务"""

    def __init__(self, db: Session):
        self.db = db

    def _get_dataframe(self) -> pd.DataFrame:
        """取全量客户 DataFrame —— 走进程级共享缓存。

        此前是本类自己 `db.query(Customer).all()` 重建一份（10 万行实测 2.8 秒），
        而 instance 由 `get_eda_service(db)` 每请求新建，self._df 缓存形同虚设。
        改为共享 `data_loader` 的缓存后，与风险引擎读的是同一份数据。

        ⚠ 返回的是共享对象，**调用方不得原地修改**（见 get_age_distribution）。
        """
        return get_cached_customer_df(self.db)

    def get_correlation_matrix(self) -> Dict[str, Any]:
        """计算相关性矩阵"""
        df = self._get_dataframe()
        numeric_cols = ["credit_score", "age", "tenure", "balance", "num_products",
                        "has_credit_card", "is_active_member", "estimated_salary",
                        "exited", "complain", "satisfaction_score", "points_earned"]

        corr_matrix = df[numeric_cols].corr()

        return {
            "features": numeric_cols,
            "matrix": corr_matrix.values.tolist()
        }

    def get_churn_analysis(self) -> Dict[str, Any]:
        """流失分析：流失客户 vs 留存客户对比"""
        df = self._get_dataframe()

        churned = df[df["exited"] == 1]
        retained = df[df["exited"] == 0]

        analysis = {
            "counts": {
                "churned": len(churned),
                "retained": len(retained)
            },
            "numeric_features": {}
        }

        numeric_features = ["age", "credit_score", "balance", "estimated_salary",
                            "tenure", "num_products", "satisfaction_score"]

        for feature in numeric_features:
            analysis["numeric_features"][feature] = {
                "churned": {
                    "mean": round(churned[feature].mean(), 2),
                    "median": round(churned[feature].median(), 2),
                    "std": round(churned[feature].std(), 2)
                },
                "retained": {
                    "mean": round(retained[feature].mean(), 2),
                    "median": round(retained[feature].median(), 2),
                    "std": round(retained[feature].std(), 2)
                }
            }

        return analysis

    def get_churn_by_category(self, feature: str) -> Dict[str, Any]:
        """按分类特征统计流失率"""
        df = self._get_dataframe()

        if feature not in ["geography", "gender", "num_products", "has_credit_card",
                           "is_active_member", "card_type", "age_group", "satisfaction_score"]:
            return {"error": f"Feature {feature} not supported"}

        result = df.groupby(feature).agg(
            total=("exited", "count"),
            churned=("exited", "sum")
        ).reset_index()

        result["churn_rate"] = round(result["churned"] / result["total"] * 100, 2)

        return {
            "feature": feature,
            "data": result.to_dict(orient="records")
        }

    def get_age_distribution(self) -> Dict[str, Any]:
        """年龄分布分析"""
        df = self._get_dataframe()

        bins = [0, 25, 35, 45, 55, 100]
        labels = ["18-25", "26-35", "36-45", "46-55", "56+"]
        # ⚠ 此前写的是 `df["age_group"] = pd.cut(...)`，会**原地修改**传入的
        # DataFrame。改为本地变量后，_get_dataframe() 才能安全地返回共享缓存
        # —— 否则这个接口一被调用，就会把共享表的 age_group 列覆盖掉，
        # 影响同时读这张表的 EDA 其他接口 / 成本收益 / 风险引擎。
        age_group = pd.cut(df["age"], bins=bins, labels=labels)

        result = df.groupby(age_group, observed=True).agg(
            total=("exited", "count"),
            churned=("exited", "sum")
        ).reset_index()
        result = result.rename(columns={"age": "age_group"})

        result["churn_rate"] = round(result["churned"] / result["total"] * 100, 2)

        return {
            "feature": "age_group",
            "data": result.to_dict(orient="records")
        }

    def get_product_overload_effect(self) -> Dict[str, Any]:
        """产品过载效应分析"""
        df = self._get_dataframe()

        result = df.groupby("num_products").agg(
            total=("exited", "count"),
            churned=("exited", "sum")
        ).reset_index()

        result["churn_rate"] = round(result["churned"] / result["total"] * 100, 2)

        return {
            "feature": "num_products",
            "data": result.to_dict(orient="records")
        }

    def get_numeric_distribution(self, feature: str) -> Dict[str, Any]:
        """数值特征分布。

        ⚠ 本方法此前**任何输入都返回 HTTP 500**（实测 age/geography/balance/
          exited/credit_score 全部 500）。两条独立的序列化缺陷：
            1) `round(np.int64(18), 2)` 返回的仍是 **np.int64**，而
               FastAPI 的 jsonable_encoder 无法序列化 numpy 标量 → ValueError；
            2) `pd.cut(...).index` 的元素是 **pandas.Interval**
               （如 `(17.926, 21.7]`），不是 JSON 基础类型 → TypeError。

          注解：`round()` 对 numpy 标量是**原位返回 numpy 类型**的，
          这与 Python 内建 int/float 的行为不同 —— 是本次踩坑的根源。

        修法：
            · 统计量一律显式 `float()` 包裹（float(np.int64) → Python float）；
            · bins 改为 `[左, 右]` 的数值对列表（并保留可读标签），
              不再直接吐 Interval 对象；
            · counts 显式转 `int()`。
        另把列名校验从"任何列都放行"改为**仅数值列**，避免对
        geography 这类字符串列做 mean/cut（会得到无意义的 nan/报错）。
        """
        df = self._get_dataframe()

        if feature not in df.columns:
            return {"error": f"Feature {feature} not found"}

        # 只接受数值列：字符串列做 mean/quantile 无意义，且 pd.cut 会失败
        if not pd.api.types.is_numeric_dtype(df[feature]):
            return {"error": f"Feature {feature} 不是数值列，无法计算分布"}

        data = pd.to_numeric(df[feature], errors="coerce").dropna()
        if len(data) == 0:
            return {"error": f"Feature {feature} 无有效数值"}

        def _f(v) -> float:
            """统一转 Python float —— 见上方缺陷说明 1。"""
            return round(float(v), 2)

        # 直方图：bins 用 [左, 右] 数值对，counts 用 int
        cats = pd.cut(data, bins=20)
        hist = cats.value_counts().sort_index()
        bins = [[float(iv.left), float(iv.right)] for iv in hist.index]
        labels = [f"{float(iv.left):.4g}~{float(iv.right):.4g}" for iv in hist.index]
        counts = [int(c) for c in hist.values]

        return {
            "feature": feature,
            "statistics": {
                "mean": _f(data.mean()),
                "median": _f(data.median()),
                "std": _f(data.std()),
                "min": _f(data.min()),
                "max": _f(data.max()),
                "q25": _f(data.quantile(0.25)),
                "q75": _f(data.quantile(0.75)),
            },
            "histogram": {
                # 保留旧键（bins/counts）以兼容既有前端，但类型已可序列化
                "bins": bins,
                "bin_labels": labels,
                "counts": counts,
            },
        }

    def get_key_insights(self) -> Dict[str, Any]:
        """动态计算关键洞察指标"""
        df = self._get_dataframe()
        total = len(df)
        overall_rate = df["exited"].mean()

        # 投诉客户流失率
        complain_df = df[df["complain"] == 1]
        complain_rate = complain_df["exited"].mean() if len(complain_df) > 0 else 0

        # 3+产品客户流失率
        high_prod_df = df[df["num_products"] >= 3]
        high_prod_rate = high_prod_df["exited"].mean() if len(high_prod_df) > 0 else 0

        # 1产品客户流失率（对比用）
        low_prod_df = df[df["num_products"] == 1]
        low_prod_rate = low_prod_df["exited"].mean() if len(low_prod_df) > 0 else 0

        # 德国流失率 vs 法国
        germany_rate = df[df["geography"] == "Germany"]["exited"].mean()
        france_rate = df[df["geography"] == "France"]["exited"].mean()

        # 非活跃 vs 活跃
        inactive_rate = df[df["is_active_member"] == 0]["exited"].mean()
        active_rate = df[df["is_active_member"] == 1]["exited"].mean()

        # 零余额流失率
        zero_bal_df = df[df["balance"] == 0]
        zero_bal_rate = zero_bal_df["exited"].mean() if len(zero_bal_df) > 0 else 0
        has_bal_df = df[df["balance"] > 0]
        has_bal_rate = has_bal_df["exited"].mean() if len(has_bal_df) > 0 else 0

        # 高龄客户流失率 (50+)
        old_df = df[df["age"] >= 50]
        old_rate = old_df["exited"].mean() if len(old_df) > 0 else 0
        young_rate = df[df["age"] < 50]["exited"].mean()

        def ratio(a, b):
            if b == 0:
                return 0
            return round(a / b, 1)

        # ── 标题按数据动态生成，不写死 ──────────────────────────
        #
        # ⚠ 实测缺陷：此前 6 条洞察的 `title` 全是**写死的字符串**
        #   （如「投诉客户流失率极高」），而 `value` 是实时算的。
        #   本数据集里 complain 与 exited 的相关性接近 0（生成器在
        #   打完流失标签后又按 0.85/0.15 重掷了 complain，把相关性洗掉了），
        #   实测：
        #       投诉客户流失率 = 20.39%   总体 = 20.42%   → 1.00 倍
        #   于是首页出现自相矛盾的一行：
        #       「投诉客户流失率极高  20.4%  是总体的 1.0 倍」
        #   同理「零余额客户流失率 13.8%（是有余额的 0.6 倍）」也挂在
        #   「风险」列表里，但它其实**低于**总体 —— 不是风险。
        #
        #   现改为按实际倍数决定标题措辞：显著高于总体才说"偏高/飙升"，
        #   接近总体就如实说"与总体持平"，低于总体则说明它**不是**风险因素。
        #   这样文案永远与数字自洽。
        def verdict_title(rate: float, base: float, subject: str,
                          high_word="流失率偏高", low_word="流失率更低") -> str:
            """按 rate/base 的倍数给出与数据相符的标题。

            ⚠ low_word 由调用方决定措辞，本函数不自动追加「（非风险因素）」——
              曾因调用方已写该后缀而输出重复：
                  「零余额客户流失率更低（非风险因素）（非风险因素）」
            """
            if base <= 0:
                return f"{subject}流失率"
            r = rate / base
            if r >= 1.5:
                return f"{subject}{high_word}"
            if r >= 1.15:
                return f"{subject}流失率略高"
            if r >= 0.85:
                return f"{subject}流失率与总体持平"
            # ⚠ 这里不追加任何固定后缀 —— 后缀的含义依 subject 而异：
            #   「零余额客户」低于是"非风险因素"，而「活跃会员」低于是"优势"。
            #   统一追加会产出「活跃会员留存优势明显（非风险因素）」这种错话。
            return f"{subject}{low_word}"

        return {
            "overall_rate": round(overall_rate * 100, 1),
            "insights": [
                {
                    "icon": "🔴",
                    "bg": "rgba(239,68,68,0.1)",
                    # ⚠ 标题随数据变化：本数据集投诉与流失几乎无关，
                    #   实测 1.00 倍，因此这里会显示"与总体持平"而非"极高"。
                    "title": verdict_title(complain_rate, overall_rate, "投诉客户"),
                    "value": f"{round(complain_rate * 100, 1)}%",
                    "sub": f"是总体的 {ratio(complain_rate, overall_rate)} 倍",
                    "detail": f"投诉客户 {len(complain_df)} 人，流失 {int(complain_df['exited'].sum())} 人",
                },
                {
                    "icon": "🟠",
                    "bg": "rgba(245,158,11,0.1)",
                    "title": verdict_title(high_prod_rate, low_prod_rate,
                                          "3+产品客户", "风险飙升"),
                    "value": f"{round(high_prod_rate * 100, 1)}%",
                    "sub": f"是1产品客户的 {ratio(high_prod_rate, low_prod_rate)} 倍",
                    "detail": f"3+产品客户 {len(high_prod_df)} 人，1产品客户流失率 {round(low_prod_rate * 100, 1)}%",
                },
                {
                    "icon": "🔵",
                    "bg": "rgba(99,102,241,0.1)",
                    "title": verdict_title(germany_rate, france_rate,
                                          "德国地区", "流失率偏高"),
                    "value": f"{round(germany_rate * 100, 1)}%",
                    "sub": f"比法国高 {ratio(germany_rate, france_rate)} 倍",
                    "detail": f"法国 {round(france_rate * 100, 1)}% · 德国 {round(germany_rate * 100, 1)}%",
                },
                {
                    "icon": "🟢",
                    "bg": "rgba(34,197,94,0.1)",
                    # 活跃是"优势"，用 inactive_rate 作基准判定
                    "title": verdict_title(active_rate, inactive_rate,
                                          "活跃会员", "流失率更高", "留存优势明显"),                    "value": f"{round(active_rate * 100, 1)}%",
                    "sub": f"非活跃 {round(inactive_rate * 100, 1)}%",
                    "detail": f"活跃客户流失率仅为非活跃的 {ratio(active_rate, inactive_rate)}",
                },
                {
                    "icon": "🟡",
                    "bg": "rgba(234,179,8,0.1)",
                    "title": verdict_title(old_rate, young_rate, "高龄客户", "流失风险"),
                    "value": f"{round(old_rate * 100, 1)}%",
                    "sub": f"50岁以上 · 是年轻的 {ratio(old_rate, young_rate)} 倍",
                    "detail": f"50+客户 {len(old_df)} 人，流失 {int(old_df['exited'].sum())} 人",
                },
                {
                    "icon": "⚪",
                    "bg": "rgba(156,163,175,0.1)",
                    # ⚠ 零余额实测流失率 13.79% **低于**有余额的 24.10%（0.6 倍）。
                    #   旧标题只写「零余额客户流失率」，配 0.6 倍，容易被读成风险；
                    #   verdict_title 会据实给出"流失率更低（非风险因素）"。
                    "title": verdict_title(zero_bal_rate, has_bal_rate,
                                          "零余额客户", "流失率偏高", "流失率更低"),
                    "value": f"{round(zero_bal_rate * 100, 1)}%",
                    "sub": f"是有余额的 {ratio(zero_bal_rate, has_bal_rate)} 倍",
                    "detail": f"零余额客户 {len(zero_bal_df)} 人，占总体 {round(len(zero_bal_df) / total * 100, 1)}%",
                },
            ]
        }

    def get_comprehensive_eda(self) -> Dict[str, Any]:
        """综合EDA分析"""
        df = self._get_dataframe()

        return {
            "overview": {
                "total_customers": len(df),
                "churned_customers": int(df["exited"].sum()),
                "churn_rate": round(df["exited"].mean() * 100, 2),
                "avg_age": round(df["age"].mean(), 1),
                "avg_balance": round(df["balance"].mean(), 2),
                "avg_salary": round(df["estimated_salary"].mean(), 2)
            },
            "correlation": self.get_correlation_matrix(),
            "churn_analysis": self.get_churn_analysis(),
            "churn_by_geography": self.get_churn_by_category("geography"),
            "churn_by_gender": self.get_churn_by_category("gender"),
            "churn_by_products": self.get_churn_by_category("num_products"),
            "age_distribution": self.get_age_distribution(),
            "product_overload": self.get_product_overload_effect()
        }


def get_eda_service(db: Session) -> EDAService:
    return EDAService(db)
