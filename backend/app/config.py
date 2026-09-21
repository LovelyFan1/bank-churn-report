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

    # ── 服务进程数 ─────────────────────────────────────────
    # ⚠ 本项目的接口里，最重的三种活儿（全量打分缓存构建、全表 pandas 扫描、
    #   10 万次 dict 构造）**是纯 Python 计算，受 GIL 限制**。实测：
    #       4 个线程并发 加速比 0.99x（完全无并行）
    #       4 个进程并发 加速比 3.88x（接近线性）
    #   所以提升并发**只能靠多进程**，线程池对本项目无效。
    #   本配置项供启动脚本/文档引用，实际生效值是 docker-compose 里的
    #   `--workers N`（uvicorn 参数，不经由 pydantic 配置生效）。
    #
    # 取值依据（6 核机器实测吞吐）：
    #   1 worker  2.5 req/s   4 worker  6.4 req/s   6 worker  7.6 req/s
    #   默认 4 —— 比单进程提升 2.5 倍，同时留 2 个核给 Celery worker。
    #
    # ⚠ 每个 worker 会各自缓存一份全量打分结果，实测 RSS ≈ 357 MB/worker。
    #   4 worker ≈ 1.43 GB。调大此值前先确认内存余量。
    WEB_WORKERS: int = 4

    # ── 成本收益口径（此前散落在各 service 里，不可审计）──
    # 全局只有**两个**独立假设，其余一律由它们推导，避免出现互相矛盾的成本模型。
    #
    # 1) 漏检成本 / 误报成本 的比值：漏掉一个真实流失客户的代价，
    #    是「白做一次干预」的多少倍。
    COST_RATIO: float = 5.0
    # 2) 单个流失客户的平均价值（元）。⚠ 业务假设值，不是从数据算出来的。
    AVG_CUSTOMER_VALUE: float = 50000.0
    #
    # 3) 挽留成功率 —— 被成功触达的流失客户里，真正被留住的比例。
    #    ⚠ 同样是**业务假设值**，且是本模块最容易漏掉的一项。
    #
    #    为什么必须有这一项（实测的公式推导）：
    #      旧实现算净收益时，TP 记 +(COST_RATIO-1)，即把「判对了」直接当成
    #      「留住了」。推导下去：
    #          ROI = reduced_loss / intervention_cost
    #              = (流失数×recall×AVG) / (触达×AVG/COST_RATIO)
    #              = COST_RATIO × precision
    #      —— 这个式子**不含任何"成功率"因子**，等价于假设
    #      「只要打电话，客户 100% 会留下」。这不成立：外呼挽留的行业
    #      公开区间大致是 20%~40%，取 100% 会把 ROI 系统性高估数倍。
    #
    #    引入 s 后的正确式子：
    #        净收益(t) = TP×(s×COST_RATIO − 1) − FP     （单位 = 一次干预成本）
    #        ROI       = COST_RATIO × precision × s
    #
    #    ⚠ 取 0.30 的依据：本系统**没有任何真实挽留结果数据**
    #      （seed_work_orders.py 造的 60 条工单是硬编码常量表，
    #        其 retained 比例是写死的，不能当作实测）。
    #      因此取行业中位偏保守值 0.30，而非从数据拟合。
    #      这是一个**必须被质疑的假设**，故在接口与页面上显式暴露，
    #      而不是藏在公式里。
    #
    #    ⚠ 连带影响（重要）：s 参与阈值选择，所以改这个值会**移动名单线**。
    #      实测（96,418 行、LightGBM、COST_RATIO=5）：
    #          s=1.00 → 最优阈值 0.20，覆盖 37.0%，ROI 2.15
    #          s=0.50 → 最优阈值 0.40，覆盖 16.1%，ROI 1.59
    #          s=0.30 → 最优阈值 0.60，覆盖  6.9%，ROI 1.21
    #      这是正确的：成功率越低，越只该去打扰最有把握的那批人。
    RETENTION_SUCCESS_RATE: float = 0.30
    #
    # 推导量（**不要**再单独配置，否则会与上面三条冲突）：
    #   单次误报成本 = 单次干预成本 = AVG_CUSTOMER_VALUE / COST_RATIO
    #   挽回收益     = 挽留成功人数 × AVG_CUSTOMER_VALUE
    #   干预投入     = 触达人次 × AVG_CUSTOMER_VALUE / COST_RATIO
    #   ⇒ ROI = COST_RATIO × precision × RETENTION_SUCCESS_RATE

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
