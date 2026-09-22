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

    # ── 智能体（对话式任务型 Agent）────────────────────────
    #
    # ⚠ 与上面三个业务假设不同，这一组是**工程配置**，不是业务口径，
    #   因此不参与任何金额/阈值计算，改它们不会移动名单线。
    #
    # ⚠ key 一律从环境变量注入（docker-compose 的 env_file 读 .env，
    #   .env 已在 .gitignore 中），**不得**写进源码或提交进仓库。
    #   留空时 /api/agent/* 返回 503 并说明原因，而不是静默降级成
    #   规则匹配 —— 后者会让调用方以为"大模型在回答"，实际没有。
    AGENT_ENABLED: bool = False
    AGENT_LLM_API_KEY: str = ""
    # 任何 OpenAI 兼容端点均可（DeepSeek / 通义 / 智谱 / Kimi …）
    AGENT_LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    AGENT_LLM_MODEL: str = "deepseek-chat"
    # 工具调用循环上限 —— 防止 LLM 反复调同一个工具陷入死循环。
    # 取 4：实测一个问题最多需要 2 次工具调用（先列表再查详情），
    # 留一倍余量即可；再大只是在掩盖"模型选错工具"这个真问题。
    AGENT_MAX_TOOL_ROUNDS: int = 4
    # 单轮对话超时（秒）。DeepSeek 实测 2~8 秒；取 60 覆盖慢网络。
    AGENT_TIMEOUT: int = 60

    # ── 内部登录系统（4A 仿真）────────────────────────────
    #
    # ⚠ 与上面三组业务假设都不同：这一组是**安全配置**，
    #   改它们不会影响任何金额、阈值或名单线。
    #
    # ⚠ 默认**开启**鉴权。关掉必须显式设 AUTH_ENABLED=false，
    #   且启动日志与 /api/auth/me 会如实反映（前端显示"鉴权已关闭"警示）。
    #   默认关会让"上线了一个没有门的系统"成为默认状态 —— 不可接受。
    AUTH_ENABLED: bool = True

    # ⚠ AUTH_SECRET_KEY 未配置时会回落到源码里的开发默认值，
    #   并打 WARNING 日志 —— 用默认密钥意味着任何人可伪造令牌。
    #   生产必须在 .env 里设置（与 AGENT_LLM_API_KEY 同样放 .env）。
    AUTH_SECRET_KEY: str = ""

    # 令牌有效期（分钟）。取 120：银行内部系统的会话通常比互联网短，
    # 但太短会让柜面/客户经理频繁重登。前端另有**无操作自动退出**
    # （见 AUTH_IDLE_MINUTES），两者是不同机制：前者是绝对上限，
    # 后者是闲置超时 —— 等保要求的"登录连接超时自动退出"指后者。
    AUTH_TOKEN_TTL_MINUTES: int = 120

    # 无操作自动退出（分钟）—— 等保三级明确要求项。
    AUTH_IDLE_MINUTES: int = 30

    # 连续失败达到该次数开始锁定（1 分钟起，递增，上限 1 小时）
    AUTH_MAX_FAILED: int = 5

    # 是否强制双因素。开时登录需两步：口令 → 动态口令。
    # 演示可关（AUTH_REQUIRE_TOTP=false）以便快速登录；
    # 但**默认开启**，否则演示不出"双因素"这个合规点。
    AUTH_REQUIRE_TOTP: bool = True

    # ── 服务间内部令牌（Agent 工具层回调自身接口用）────────
    #
    # ⚠ 为什么需要它：Agent 的工具层是**薄 HTTP 封装**（见 agent/tools.py
    #   的设计说明），它用 urllib 请求 `http://127.0.0.1:8000/api/...`。
    #   引入鉴权后，这些**内部调用没人带令牌** → 全部 401，表现为
    #   「Agent 查不到任何客户」（实测：propose 报
    #   `HTTP 401: 未登录或缺少令牌`）。这是服务间认证的经典问题。
    #
    # 解法：内部令牌 + **限定回环来源**。两者必须同时满足才放行 ——
    #   只认令牌 → 令牌泄露即全网可调；只认回环 → 容器内任何进程可绕鉴权。
    #   叠加后，攻击面从"网络可达"收缩到"已能在容器内执行代码"。
    #
    # ⚠ 留空时由 AUTH_SECRET_KEY 派生（同一把钥匙，少一个要管的秘密）。
    #   生产必须设置 AUTH_SECRET_KEY。
    AUTH_INTERNAL_TOKEN: str = ""

    # 演示账号口令（仅播种用）。首次登录后应修改。
    # ⚠ 放在配置而非硬编码在播种脚本里，便于演示时统一改。
    AUTH_DEMO_PASSWORD: str = "Bank@2026"

    # ── 演示辅助：在登录页显示动态口令 ─────────────────────
    #
    # ⚠ 默认**关闭**。开启后登录页显示当前 TOTP 口令与绑定密钥，
    #   目的是让演示者不必依赖命令行或手机 App —— 否则演示前要先跑一条
    #   docker exec 取码，或者用 App 绑定（换台机器就要重绑）。
    #
    # ⚠ 必须知道的代价：这等于把第二因子**公开在页面上**。在开启的环境里，
    #   双因素对"能打开登录页的人"不再构成额外门槛。
    #   故只适用于演示/评审环境，**绝不可用于生产**。
    #   开启时前端显示醒目警示；接口也只在开启时返回这些字段。
    AUTH_DEMO_SHOW_TOTP: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
