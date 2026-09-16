from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    APP_NAME: str = "Bank Customer Churn Analysis System"
    VERSION: str = "1.0.0"
    DEBUG: bool = True

    # Database
    DATABASE_URL: str = "sqlite:///./churn_analysis.db"

    # Redis / Celery
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"

    # Data generation
    NUM_CUSTOMERS: int = 10000
    CHURN_RATE: float = 0.2037

    # ── 数据来源（第四个独立配置，决定系统启动时灌哪份数据）──────
    #
    # "generator" = 用 data_generator 现算合成数据（默认；无条件可跑，
    #               不依赖任何外部文件，适合离线/演示兜底）
    # "csv"       = 从 DATA_CSV_PATH 读一份 external CSV（标定数据 10 万条）
    #
    # 换成 csv 前必须知道的两件事：
    #   1) 客户编号会在两套数据间**完全重叠但指向不同的人**
    #      （都是 C000001~C0xxxxx，但不是同一批客户）。所以切换数据源时
    #      必须清空 customers 表，否则会出现「同一 ID 两套记录混在一起」。
    #   2) 已有的 work_orders 会因客户被换掉而变成悬空引用
    #      （工单上写着张三，点进去是李四）。startup 会检查并报警。
    DATA_SOURCE: str = "generator"
    DATA_CSV_PATH: str = ""
    # CSV 导出的列若多于 Customer 模型，只取模型认识的列（其余忽略）；
    # 若少于模型所需，缺的列填空。两条都不会报错 —— 但会在日志里说明。
    DATA_SOURCE_NAME: str = ""

    # CORS
    CORS_ORIGINS: list = ["http://localhost:5173", "http://localhost:3000"]

    # ML Models
    RANDOM_STATE: int = 42
    TEST_SIZE: float = 0.2
    CV_FOLDS: int = 5

    # ── 成本收益口径（此前散落在各 service 里，不可审计）──
    # 全局只有**两个**独立假设，其余一律由它们推导，避免出现互相矛盾的成本模型。
    #
    # 1) 漏检成本 / 误报成本 的比值：漏掉一个真实流失客户的代价，
    #    是「白做一次干预」的多少倍。
    COST_RATIO: float = 5.0
    # 2) 单个流失客户的平均价值（元）。⚠ 业务假设值，不是从数据算出来的。
    AVG_CUSTOMER_VALUE: float = 50000.0
    #
    # 推导量（**不要**再单独配置，否则会与上面两条冲突）：
    #   单次误报成本 = 单次干预成本 = AVG_CUSTOMER_VALUE / COST_RATIO
    #   挽回收益     = 挽留成功人数 × AVG_CUSTOMER_VALUE
    #   干预投入     = 触达人次 × AVG_CUSTOMER_VALUE / COST_RATIO
    #   ⇒ ROI = COST_RATIO × (挽留成功 / 触达人次) = COST_RATIO × 精准率

    # ── 客户价值分层（第三个独立假设）──────────────────────
    # ⚠ 同样是**业务假设值**，不是从数据算出来的。
    #
    # 界定「客户值多少钱」。此前系统只有「流失概率」一个维度，决策退化成
    # 「按概率分四档」——不管客户值 20 万还是 0，都走同一套动作。实测：模型
    # 概率榜 Top100 里 37 人余额为 0，真实流失者 44% 余额为 0，这些人抓到了
    # 也无资产可留。
    #
    # 注意 balance 在这里的角色是**定义损失**，不是**预测流失**：
    # balance 与 exited 的相关性只有 r=-0.078，作为预测特征几乎是噪声（所以
    # 模型不靠它）；但「余额 0 的客户流失，银行损失多少」是定义问题，答案就是 0。
    # 两件事不矛盾，分工是：模型管「会不会跑」，本项管「跑了值多少」。
    VALUE_TIER_HIGH: float = 100000.0
    #
    # 推导量：价值层由 balance 单变量划分，三档互斥且穷尽
    #   balance >= VALUE_TIER_HIGH  → HIGH  走高价值渠道（客户经理 1 对 1）
    #   0 < balance < VALUE_TIER_HIGH → LOW   走低成本渠道（主动外呼）
    #   balance == 0                 → ZERO  走近零成本渠道（APP 推送/短信）
    # 期望价值 = 流失概率 × 余额，供排序用（零余额者恒为 0，自然沉底）

    # Chunked data loading (for 10M+ scale)
    DATA_CHUNK_SIZE: int = 50000

    class Config:
        env_file = ".env"


settings = Settings()
