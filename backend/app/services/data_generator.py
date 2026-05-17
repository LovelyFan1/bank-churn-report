import numpy as np
import pandas as pd
from sqlalchemy.orm import Session
from app.models.customer import Customer
from app.config import settings


class DataGenerator:
    """
    数据生成器 - 基于真实Kaggle Bank Customer Churn数据集特征

    真实数据集统计（参考Kaggle）:
    - 样本量: ~10,000
    - 流失率: ~20.4%
    - 平均年龄: ~39岁
    - 平均信用评分: ~650
    - 平均余额: ~76,486
    - 平均任期: ~5年
    """

    def __init__(self):
        self.n_customers = settings.NUM_CUSTOMERS
        self.churn_rate = settings.CHURN_RATE
        self.random_state = settings.RANDOM_STATE

    def generate(self) -> pd.DataFrame:
        rng = np.random.default_rng(self.random_state)

        n = self.n_customers
        n_churned = int(n * self.churn_rate)

        # 先生成投诉数据（真实数据中投诉与流失高度相关）
        # 约15%的客户有投诉
        complain = rng.choice([0, 1], n, p=[0.85, 0.15])

        data = {
            "row_number": range(1, n + 1),
            "customer_id": [f"C{i:06d}" for i in range(1, n + 1)],
            "surname": self._generate_surnames(n, rng),
            "credit_score": self._generate_credit_scores(n, rng),
            "geography": self._generate_geography(n, rng),
            "gender": rng.choice(["Male", "Female"], n, p=[0.55, 0.45]),
            "age": self._generate_ages(n, rng),
            "tenure": self._generate_tenure(n, rng),
            "balance": self._generate_balances(n, rng),
            "num_products": self._generate_num_products(n, rng),
            "has_credit_card": rng.choice([0, 1], n, p=[0.3, 0.7]),
            "is_active_member": rng.choice([0, 1], n, p=[0.48, 0.52]),
            "estimated_salary": self._generate_salary(n, rng),
            "exited": np.zeros(n, dtype=int),
            "complain": complain,
            "satisfaction_score": rng.integers(1, 6, n),
            "card_type": rng.choice(["SILVER", "GOLD", "PLATINUM", "DIAMOND"], n, p=[0.3, 0.35, 0.25, 0.1]),
            "points_earned": rng.integers(100, 1000, n),
        }

        df = pd.DataFrame(data)

        # Set churn labels with correlation to features
        churn_indices = self._select_churn_indices(df, n_churned, rng)
        df.loc[churn_indices, "exited"] = 1

        # 重新设置投诉数据，使其与流失高度相关
        # 流失客户中85%有投诉，留存客户中15%有投诉
        churned_mask = df["exited"] == 1
        retained_mask = df["exited"] == 0
        df.loc[churned_mask, "complain"] = rng.choice([0, 1], churned_mask.sum(), p=[0.15, 0.85])
        df.loc[retained_mask, "complain"] = rng.choice([0, 1], retained_mask.sum(), p=[0.85, 0.15])

        # Derived features
        df["age_group"] = pd.cut(df["age"], bins=[0, 25, 35, 45, 55, 100], labels=["18-25", "26-35", "36-45", "46-55", "56+"])
        df["balance_salary_ratio"] = df["balance"] / (df["estimated_salary"] + 1)

        return df

    def _generate_surnames(self, n: int, rng: np.random.Generator) -> list:
        surnames = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
                     "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
                     "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
                     "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson"]
        return list(rng.choice(surnames, n))

    def _generate_credit_scores(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """信用评分: 正态分布，均值650，标准差100（真实数据特征）"""
        scores = rng.normal(650, 100, n)
        return np.clip(scores, 350, 850).astype(int)

    def _generate_geography(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """地理位置: 法国50%，德国25%，西班牙25%（真实数据分布）"""
        return rng.choice(["France", "Germany", "Spain"], n, p=[0.50, 0.25, 0.25])

    def _generate_ages(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """年龄: 正态分布，均值39，标准差10（真实数据特征）"""
        ages = rng.normal(39, 10, n)
        return np.clip(ages, 18, 92).astype(int)

    def _generate_tenure(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """任期: 均匀分布0-10年（真实数据特征）"""
        return rng.integers(0, 11, n)

    def _generate_balances(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """
        账户余额: 双峰分布
        - 约36%客户余额为0（真实数据特征）
        - 其余客户余额正态分布，均值约120,000
        """
        n_zero = int(n * 0.36)
        n_positive = n - n_zero

        balances = np.zeros(n)
        positive_balances = rng.normal(120000, 50000, n_positive)
        balances[n_zero:] = np.clip(positive_balances, 0, 250000)
        rng.shuffle(balances)
        return balances

    def _generate_num_products(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """产品数量: 1个产品50%，2个30%，3个15%，4个5%（真实数据分布）"""
        return rng.choice([1, 2, 3, 4], n, p=[0.50, 0.30, 0.15, 0.05])

    def _generate_salary(self, n: int, rng: np.random.Generator) -> np.ndarray:
        """预估薪资: 均匀分布10,000-200,000（真实数据特征）"""
        return rng.uniform(10000, 200000, n)

    def _select_churn_indices(self, df: pd.DataFrame, n_churned: int, rng: np.random.Generator) -> list:
        """
        选择流失客户索引 - 基于真实数据中的流失因素

        真实数据中的关键流失因素:
        1. 年龄: 年龄越大流失概率越高
        2. 投诉: 投诉与流失高度相关
        3. 产品数量: 3+产品有"产品过载"效应
        4. 活跃状态: 非活跃会员流失率更高
        5. 余额: 零余额客户流失率略高
        """
        scores = np.zeros(len(df))

        # Age factor (higher age = higher churn probability)
        scores += (df["age"] - 30) * 0.02

        # Complaint factor (strong correlation)
        scores += df["complain"] * 2.0

        # Num products factor (3+ products = "product overload")
        scores += (df["num_products"] >= 3).astype(float) * 1.5

        # Inactive member factor
        scores += (1 - df["is_active_member"]) * 0.8

        # Low balance factor
        scores += (df["balance"] == 0).astype(float) * 0.5

        # Germany has slightly higher churn rate in real data
        scores += (df["geography"] == "Germany").astype(float) * 0.3

        # Normalize scores to probabilities
        scores = (scores - scores.min()) / (scores.max() - scores.min() + 1e-10)
        probabilities = scores / scores.sum()

        # 使用argsort选择得分最高的n_churned个索引（避免浮点数精度问题）
        sorted_indices = np.argsort(probabilities)[::-1]
        churn_indices = sorted_indices[:n_churned].tolist()
        return churn_indices

    def save_to_db(self, db: Session, df: pd.DataFrame):
        """批量保存到数据库（性能优化）"""
        records = df.to_dict(orient="records")
        db.bulk_save_objects([Customer(**record) for record in records])
        db.commit()


def get_data_generator() -> DataGenerator:
    return DataGenerator()
