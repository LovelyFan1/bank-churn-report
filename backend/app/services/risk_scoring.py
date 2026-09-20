"""风险评分引擎 — 分位数分级 + 成本收益参考阈值。

统一的风险评估标准，供客户列表、风险预测、批量预测、成本收益共用。

设计说明:
- 树模型(LightGBM)的 predict_proba 绝对值未经校准且趋于两极(叶子纯度高)，
  用 isotonic / Platt 校准反而会放大成「一堆 1.0 / 0.95」，导致分级严重失衡。
- 但模型概率的**相对排序是可靠的**(AUC≈0.787)，因此采用**原始概率的分位数**分级，
  保证各级占比稳定、排序正确，规避校准失真。

分级(默认):
  CRITICAL: 原始概率 >= P95  (约 top 5%)
  HIGH    : >= P70           (约 top 30%)
  MEDIUM  : >= P35           (约 top 65%)
  LOW     : 其余             (约 35%)

成本收益最优阈值作为「建议干预线」参考信息返回，不参与分级。

客户价值层（与风险等级正交的第二个维度）:
  风险等级回答「会不会跑」，价值层回答「跑了值多少」。两者交叉才决定该做什么：
  同样一个 HIGH，高价值客户要客户经理 1 对 1，零余额客户一条 APP 推送即可。
  见 config.VALUE_TIER_HIGH 的说明 —— balance 在此用于**定义损失**而非预测流失。
"""

import json
import logging
import time
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from app.config import settings
from app.services.data_loader import prepare_features, get_cached_customer_df

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"

# 漏检成本 / 误报成本 比值 —— 口径统一由 config 提供，避免与 cost_benefit 漂移
COST_RATIO = settings.COST_RATIO

# 分位数边界（百分位）
P_CRITICAL = 95   # top 5%
P_HIGH = 70       # top 30%
P_MEDIUM = 35     # top 65%

CACHE_TTL = 600  # 秒

# ⚠ 已知行为（预先存在，非本次改动引入）：模型重训后本模块的缓存不会立即失效。
#   Celery worker 是**独立进程**，它写的 saved_models/ 与本进程的缓存互不可见，
#   且 _best_model_cache 没有 TTL。故重训完成后最多需等 CACHE_TTL(10 分钟)
#   才会用上新模型；期间界面显示的仍是旧模型的分级。
#   若要立即生效：重启 backend 容器，或调用下方 reset_caches()。
#   （本次性能改动**没有**触碰这段逻辑 —— 它涉及模型切换语义，属另一个话题。）


def reset_caches() -> None:
    """清空本进程的全部评分缓存，使下次请求重新加载模型、重新全量打分。

    供「重训完成后需要立即生效」的场景调用。跨进程无法自动触发
    （Celery worker 与 API 不是一个进程），故留作显式接口。
    """
    global _engine_cache, _scored_cache
    _best_model_cache["name"] = None
    _best_model_cache["model"] = None
    _best_model_cache["ts"] = 0.0
    _engine_cache = {"df": None, "raw": None, "thresholds": None,
                     "optimal_threshold": None, "name": None, "ts": 0.0}
    _scored_cache = {"data": None, "name": None, "ts": 0.0}


# ── 进程级缓存 ───────────────────────────────────────────
#
# ⚠ _best_model_cache 带 TTL 的原因：
#   模型对象此前**永不过期**（键只有 name/model，没有时间戳）。而本服务跑在
#   多 worker 部署下（docker-compose: uvicorn --workers 4），每个 worker 各自
#   持有一份缓存，Celery 重训完写新模型后，没有任何机制通知这些 worker。
#   结果：重训后，已加载过模型的 worker 会**无限期沿用旧模型**，直到容器重启；
#   而没加载过的 worker 用新模型 —— 同一个客户在不同请求间得到不同等级，
#   表现为"刷新几次结果不一样"。
#   加 TTL 后最多 MODEL_CACHE_TTL 秒收敛到新模型，无需重启。
MODEL_CACHE_TTL = 600  # 秒

_best_model_cache = {"name": None, "model": None, "ts": 0.0}
_engine_cache = {
    "df": None,                # 全量数据（复用，避免重复 load_all）
    "raw": None,               # 全量原始概率
    "thresholds": None,        # {"critical", "high", "medium"} 分位数
    "optimal_threshold": None,  # 成本收益参考阈值
    "name": None,
    "ts": 0.0,
}
_scored_cache = {"data": None, "name": None, "ts": 0.0}


# ── 模型加载（缓存）─────────────────────────────────────

def _cache_expired() -> bool:
    """模型对象缓存是否已过期（或从未加载）。"""
    if _best_model_cache["model"] is None:
        return True
    return time.time() - _best_model_cache["ts"] >= MODEL_CACHE_TTL


def _load_best_model():
    """加载 AUC 最高的模型，进程级缓存（带 TTL）。返回 (model, name)。

    ⚠ 读取失败要**记录并降级**，不能向上抛：
      本函数被客户列表/风险预测/成本收益/矩阵共同依赖。若 meta.json 正被
      训练任务写入（旧实现是非原子写，见 train.py 的说明），json.load 或
      joblib.load 会抛异常。旧实现没有 try —— 异常会一路冒到接口层变成 500，
      或者被更上层吞掉变成空数据。这里统一转成 (None, None)，由调用方按
      "模型未就绪"处理，并留下 warning 便于定位。
    """
    if not _cache_expired():
        return _best_model_cache["model"], _best_model_cache["name"]

    import joblib

    meta_path = MODEL_DIR / "meta.json"
    if not meta_path.exists():
        return None, None

    # 读取失败大概率是撞上训练写入窗口，短退避重试几次
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
                time.sleep(0.15 * (attempt + 1))

    if meta is None:
        logger.warning(
            "risk_scoring: 读取 %s 失败（已重试 %d 次）: %s: %s",
            meta_path, ATTEMPTS, type(last_err).__name__, last_err,
        )
        return None, None

    best_name = max(meta["results"].keys(), key=lambda x: meta["results"][x]["auc"])
    model_path = MODEL_DIR / (best_name.lower().replace(" ", "_") + ".joblib")
    if not model_path.exists():
        return None, None

    try:
        model = joblib.load(model_path)
    except Exception as e:
        # joblib 读到半截文件会抛（pickle 反序列化失败），同样降级处理
        logger.warning("risk_scoring: 加载模型 %s 失败: %s: %s", model_path, type(e).__name__, e)
        return None, None

    _best_model_cache["model"] = model
    _best_model_cache["name"] = best_name
    _best_model_cache["ts"] = time.time()
    return model, best_name


# ── 成本收益最优阈值（参考）─────────────────────────────

def net_profit(tp: int, fp: int, fn: int, cost_ratio: float = COST_RATIO) -> float:
    """二分类混淆矩阵 → 净收益，单位为「一次误报（干预）的成本」。

    这是全系统**唯一**的成本模型，risk_scoring 与 cost_benefit 共用，
    以免两处各写一套、得出互相打架的结论。

    记账基准是「不干预」：不管我们做不做，没被识别出的流失客户（FN）都会流失，
    FN 相对于该基准不产生额外损失，故不计入。以一次干预成本 c 为单位：

        TP（劝住了本来会流失的人）= 保住客户价值 - 干预成本 = +(cost_ratio - 1)
        FP（白跑一趟）             = -干预成本              = -1
        TN（本来就不会流失）       = 0
        FN                         = 0（与基准同为流失，无增量差异）

    等价换算（cost_ratio = 客户价值 / 单次干预成本）：
        净收益 = TP×客户价值 - 触达人次×单次干预成本，再除以 c。

    ⚠ 旧实现在用 `tp*1 - fn*cost_ratio - fp*1`：把「劝住一个」记 +1、
    却把「漏掉一个」记 -cost_ratio，两者不对称 —— 漏检背了 5 倍代价，
    TP 却只拿 1 倍收益，导致所有阈值下净收益恒为负，「最优阈值」退化成
    「最不亏的那一档」，且与 `reduced_loss` 口径互相矛盾。
    改为上式后，TP 与「客户价值」直接挂钩，两者才可比。
    """
    return tp * (cost_ratio - 1) - fp


def _optimal_threshold(raw_proba: np.ndarray, y_true: np.ndarray) -> float:
    """遍历阈值，返回净利润最大化的二分类阈值。"""
    best_t, best_profit = 0.5, -float("inf")
    for t in np.arange(0.05, 0.96, 0.05):
        pred = (raw_proba >= t).astype(int)
        tp = int(np.sum((pred == 1) & (y_true == 1)))
        fp = int(np.sum((pred == 1) & (y_true == 0)))
        fn = int(np.sum((pred == 0) & (y_true == 1)))
        profit = net_profit(tp, fp, fn)
        if profit > best_profit:
            best_profit, best_t = profit, float(t)
    return best_t


# ── 引擎构建（缓存）─────────────────────────────────────

def _ensure_engine(db: Session):
    """加载模型 + 全量打分 + 计算分位数阈值 + 成本收益参考。返回缓存 dict 或 None。"""
    if _engine_cache["raw"] is not None and time.time() - _engine_cache["ts"] < CACHE_TTL:
        return _engine_cache

    model, name = _load_best_model()
    if model is None:
        return None

    # 走共享缓存 —— 与 EDA / 成本收益读同一份表，避免三处各读一遍
    # （10 万行实测每次约 2.8 秒）。⚠ 该函数返回共享对象，此处只读不改。
    df = get_cached_customer_df(db)
    X, y, _ = prepare_features(df)
    raw = model.predict_proba(X)[:, 1]

    # 分位数阈值（全量原始概率）
    thresholds = {
        "critical": float(np.percentile(raw, P_CRITICAL)),
        "high": float(np.percentile(raw, P_HIGH)),
        "medium": float(np.percentile(raw, P_MEDIUM)),
    }

    # 成本收益参考阈值（测试集上计算，避免过拟合）
    from sklearn.model_selection import train_test_split
    idx = np.arange(len(df))
    _, test_idx = train_test_split(
        idx, test_size=settings.TEST_SIZE,
        random_state=settings.RANDOM_STATE, stratify=y,
    )
    t_opt = _optimal_threshold(raw[test_idx], y[test_idx])

    _engine_cache.update({
        "df": df,
        "raw": raw,
        "thresholds": thresholds,
        "optimal_threshold": t_opt,
        "name": name,
        "ts": time.time(),
    })
    return _engine_cache


# ── 分级 ────────────────────────────────────────────────

def _level(p: float, thresholds: dict) -> str:
    if p >= thresholds["critical"]:
        return "CRITICAL"
    if p >= thresholds["high"]:
        return "HIGH"
    if p >= thresholds["medium"]:
        return "MEDIUM"
    return "LOW"


def _legacy_level(prob: float) -> str:
    """模型未训练时的固定阈值回退。"""
    if prob >= 0.7:
        return "CRITICAL"
    if prob >= 0.3:
        return "HIGH"
    if prob >= 0.1:
        return "MEDIUM"
    return "LOW"


# ── 客户价值层 ───────────────────────────────────────────

# 每个价值层对应的触达渠道。渠道随价值层固定，不随风险等级变化 ——
# 高价值客户无论哪个等级都不该只收到一条短信，零余额客户无论多紧急
# 也不值得占用客户经理工时（实测紧急档里零余额占 40%）。
CHANNEL_BY_TIER = {
    "HIGH": "relationship",   # 客户经理 1 对 1
    "LOW": "outbound",        # 主动外呼
    "ZERO": "automated",      # APP 推送 / 短信（近零成本）
}


def value_tier(balance: float) -> str:
    """按余额划分价值层：HIGH / LOW / ZERO。

    与 _level() 正交。边界取自 settings.VALUE_TIER_HIGH（业务假设值）。
    三档互斥且穷尽，所以任意客户必属且仅属一层。
    """
    if balance is None:
        return "ZERO"
    if balance <= 0:
        return "ZERO"
    if balance >= settings.VALUE_TIER_HIGH:
        return "HIGH"
    return "LOW"


def expected_value(prob: float, balance: float) -> float:
    """期望价值 = 流失概率 × 余额，单位为「元」。

    用于排序干预优先级。含义是「这个客户如果流失，期望损失多少」——
    零余额客户恒为 0，因此自然沉到名单末尾，不需要额外的排除规则。

    ⚠ 与 AVG_CUSTOMER_VALUE 全局常数不同：那个是「平均一个客户值多少」，
    这里是「这一个客户值多少」，因此排序结果不能用纯概率替代。实测按此排序
    比纯概率排序，Top100 名单只重合 56%，捕获的可挽回价值从 27.9% 提高到 43.0%。
    """
    if balance is None or balance <= 0 or prob is None:
        return 0.0
    return round(float(prob) * float(balance), 2)


# ── 干预策略（全系统单一来源）────────────────────────────
#
# 此前系统有**三套互不相同**的策略来源：本模块的 FACTOR_STRATEGY_MAP、
# celery predict.py 里一份几乎重复的拷贝、以及前端 InterventionStrategy.vue
# 里另写的一套 ACTION_BY_TIER，建单下拉还有第三套写死选项。结果是同一个客户
# 在客户列表、干预策略页、建单弹窗三处看到三个不同的"推荐动作"。
#
# 统一后的分工（**渠道、动作、理由三者各有其决定因素，不可互换**）：
#
#   价值层   → 决定【渠道】  硬规则。零余额客户永远走自动化触达 ——
#                            给他打电话，人工成本照花，可挽回资产是 0。
#   风险等级 → 决定【动作】  软规则。同样是高危，高价值客户要客户经理上门，
#                            低价值客户一条外呼即可。
#   风险因素 → 决定【理由】  解释性，**不参与选策略**。它回答"为什么调用这个
#                            动作"，供客服在通话前了解背景。

# 渠道 → 该渠道下的动作强度表。键是 (价值层, 风险等级)。
# 这里写的是**默认建议**；建单时可覆盖，但覆盖需留原因（见 work_orders 路由）。
_ACTION_TABLE = {
    "HIGH": {
        "CRITICAL": "客户经理上门 + 定制挽留方案",
        "HIGH": "客户经理 1 对 1 回访",
        "MEDIUM": "客户经理电话 + 权益提醒",
        "LOW": "专属权益推送",
    },
    "LOW": {
        "CRITICAL": "优先外呼 + 优惠方案",
        "HIGH": "主动外呼关怀",
        "MEDIUM": "外呼 + 短信组合触达",
        "LOW": "短信关怀",
    },
    "ZERO": {
        "CRITICAL": "APP 推送唤醒",
        "HIGH": "APP 推送 + 权益引导",
        "MEDIUM": "自动化营销触达",
        "LOW": "常规内容推送",
    },
}

# 风险因素 → 理由短语。取**首个匹配**（按列表顺序），不再像旧实现那样
# 让靠前的条目永久遮蔽靠后的条目。
_REASON_MAP = [
    (["投诉"], "近期有投诉记录，需优先安抚"),
    (["产品超载"], "产品数量过多，存在体验负担"),
    (["非活跃"], "长期不活跃，服务感知弱"),
    (["余额为零"], "账户已空置，需重新建立连接"),
    (["德国"], "所在地区整体流失率偏高"),
    (["高龄"], "高龄客户对服务变动更敏感"),
    (["满意度"], "满意度评分偏低，存在明确不满"),
    (["信用"], "信用评分偏低，风险敞口较大"),
    (["在网"], "在网时间较短，尚未形成使用习惯"),
]


def recommend_action(tier: str, level: str, factors: list | None = None) -> dict:
    """产出干预建议 —— 全系统唯一的策略来源。

    返回 {"channel", "action", "reason"}：
      channel  触达渠道，由**价值层**硬定（automated / outbound / relationship）
      action   具体动作，由**价值层 × 风险等级**决定
      reason   建议理由，由**风险因素**取首个匹配；无匹配时给中性默认

    调用方（客户列表、批量打分、干预策略页、建单弹窗）必须都用这一个函数，
    否则又会出现"同一客户三个说法"。
    """
    tier = tier if tier in _ACTION_TABLE else "LOW"
    level = level if level in ("CRITICAL", "HIGH", "MEDIUM", "LOW") else "LOW"

    # factors 可能来自 JSON 反序列化或调用方误传，未必是字符串列表 ——
    # 强制归一化，避免 `kw in f` 在非可迭代对象上抛异常（本函数是全系统
    # 唯一策略来源，不能因为一个脏入参就让整个客户列表接口挂掉）。
    if not isinstance(factors, (list, tuple)):
        factors = []
    factors = [f for f in factors if isinstance(f, str)]

    reason = "常规维护建议"
    for keywords, text in _REASON_MAP:
        if any(kw in f for f in factors for kw in keywords):
            reason = text
            break

    return {
        "channel": CHANNEL_BY_TIER[tier],
        "action": _ACTION_TABLE[tier][level],
        "reason": reason,
    }


# 渠道中文名 —— 供前端下拉与详情页复用，避免各处各写一份
CHANNEL_LABELS = {
    "relationship": "客户经理 1 对 1",
    "outbound": "主动外呼",
    "automated": "APP 推送 / 短信",
}

# 渠道 → 对应的价值层。渠道被人工覆盖后，需要据此反查动作表。
_CHANNEL_TIER = {v: k for k, v in CHANNEL_BY_TIER.items()}


def action_for_channel(channel: str, level: str) -> str:
    """给定渠道与风险等级，返回该渠道下的动作。

    用于**渠道被人工覆盖**后重算动作 —— 否则会出现「渠道写客户经理、
    动作却还是 APP 推送」这种新的自相矛盾。
    """
    tier = _CHANNEL_TIER.get(channel)
    if tier is None:
        return ""
    return _ACTION_TABLE[tier].get(level, "")


def _risk_factors(row) -> list:
    factors = []
    if row.get("complain") == 1:
        factors.append("有投诉记录")
    if row.get("is_active_member") == 0:
        factors.append("非活跃用户")
    if int(row.get("num_products", 0) or 0) >= 3:
        factors.append(f"持有 {int(row['num_products'])} 个产品（产品超载）")
    if int(row.get("age", 0) or 0) >= 50:
        factors.append(f"高龄客户（{int(row['age'])} 岁）")
    if float(row.get("balance", 0) or 0) == 0:
        factors.append("账户余额为零")
    if row.get("geography") == "Germany":
        factors.append("德国地区客户")
    if int(row.get("satisfaction_score", 5) or 5) <= 2:
        factors.append(f"满意度偏低（{int(row['satisfaction_score'])}/5）")
    if int(row.get("credit_score", 850) or 850) < 600:
        factors.append(f"信用评分偏低（{int(row['credit_score'])} 分）")
    tenure = row.get("tenure")
    if tenure is not None and int(tenure) <= 2:
        factors.append(f"在网时长较短（{int(tenure)} 年）")
    return factors


# ── 全量打分（缓存）─────────────────────────────────────
#
# ⚡ 本段是**向量化**实现的。原实现是 `for i in range(len(df)): row = df.iloc[i]`
#    逐行循环，实测 10 万行要 11.4s。慢的原因不是「Python 慢」，而是
#    `df.iloc[i]` 每取一行都会**构造一个 pandas Series 对象**，再从里面查值。
#    实测分解（10 万行）：
#        _risk_factors（逐行 Series.get）   7.03s
#        df.iloc[i] 取字段                   约 5.2s
#        recommend_action 10 万次            0.31s
#        物化 23 字段 dict                   0.59s
#    改成向量化后循环段 11.4s → 0.95s（12.2x）。
#
# ⚠⚠ 两个必须遵守的约束（改动这段前务必先读）：
#
#   1) **概率必须先转 float64 再舍入。** `engine["raw"]` 是 **float32**，
#      `np.round(raw, 4)` 在 float32 里舍入，与 `round(float(raw[i]), 4)`
#      的结果**在 99877/100000 条上不同**（打印出来都是 0.1603，看不出差别，
#      但值不同）。必须 `raw.astype(np.float64)`。
#
#   2) **输出的 key 顺序不能变。** 前端 / 导出 / 工单快照都依赖这个顺序。
#      `_SCORED_KEYS` 显式写死，不要依赖 dict 字面量的书写顺序。
#      实测契约：id…risk_factors 共 23 个键，改前后逐字节一致。
#
#   向量化"只快不降内存"—— `_scored_cache` 仍存 10 万个 dict（实测 264 MB），
#   因为缓存本来就是给列表/导出接口直接复用的。若要降内存，需要一并改
#   下游消费方，属于另一个话题。

_SCORED_KEYS = (
    "id", "customer_id", "surname", "geography", "gender", "age", "tenure",
    "balance", "num_products", "is_active_member", "credit_score",
    "estimated_salary", "satisfaction_score", "exited", "probability",
    "risk_level", "value_tier", "expected_value",
    "channel", "strategy", "action", "reason", "risk_factors",
)

# `_REASON_MAP` 的键 → 对应的向量化命中掩码，在 _build_scored_result 内按需构建。
# 之所以不直接在遍历里按关键词判断：关键词是字符串、掩码是布尔数组，
# 二者无法在向量层比较，必须预先建立映射。


def _build_scored_result(df, raw, thresholds) -> list[dict]:
    """向量化地把 (df, raw, thresholds) 转成与旧实现**逐字段一致**的 list[dict]。

    调用方应保证 df 的长度与 raw 一致、且 thresholds 非空。
    """
    n = len(df)

    # ── 取列成 ndarray（这一步几乎免费：0.0003s）──────
    # astype(np.float64) 是必须的，见上方约束 1
    bal = df["balance"].to_numpy().astype(np.float64)
    p = raw.astype(np.float64)
    tenure = df["tenure"].to_numpy()
    nprod = df["num_products"].to_numpy()
    active = df["is_active_member"].to_numpy()
    age = df["age"].to_numpy()
    sat = df["satisfaction_score"].to_numpy()
    credit = df["credit_score"].to_numpy()

    # ── 两个维度的分级（各 0.005s）─────────────────────
    # 价值层：与 value_tier() 同口径。balance<=0 → ZERO，>=VALUE_TIER_HIGH → HIGH
    tier = np.where(bal <= 0, "ZERO",
                    np.where(bal >= settings.VALUE_TIER_HIGH, "HIGH", "LOW"))
    # 风险等级：与 _level() 同口径，注意边界是 >=
    lvl = np.where(p >= thresholds["critical"], "CRITICAL",
                   np.where(p >= thresholds["high"], "HIGH",
                            np.where(p >= thresholds["medium"], "MEDIUM", "LOW")))
    # 期望价值：与 expected_value() 同口径（零余额/无概率 → 0.0）
    # 实测 np.round(x,2) 与 Python round(x,2) 在 10 万个值上差异 0 条
    ev = np.round(np.where(bal <= 0, 0.0, p * bal), 2)

    # ── 风险因素：9 个布尔掩码（0.004s）────────────────
    # 顺序**必须**与 _risk_factors() 里的 append 顺序一致 ——
    # risk_factors 是个有序列表，`_REASON_MAP` 依赖它取首个匹配
    m1 = df["complain"].to_numpy() == 1          # 有投诉记录
    m2 = active == 0                              # 非活跃用户
    m3 = nprod >= 3                               # 产品超载
    m4 = age >= 50                                # 高龄
    m5 = bal == 0                                 # 余额为零
    m6 = df["geography"].to_numpy() == "Germany"  # 德国地区
    m7 = sat <= 2                                 # 满意度偏低
    m8 = credit < 600                             # 信用评分偏低
    m9 = tenure <= 2                              # 在网时长较短
    masks = [m1, m2, m3, m4, m5, m6, m7, m8, m9]

    # ── 理由：按 _REASON_MAP 顺序取首个匹配 ────────────
    # 倒序填充 → 靠前的条目后写、覆盖靠后的，等价于"取首个匹配"。
    # 关键词必须能唯一映射到掩码，故用显式映射表 + 运行时断言兜底。
    _kw_to_mask = {
        "投诉": m1, "产品超载": m3, "非活跃": m2, "余额为零": m5,
        "德国": m6, "高龄": m4, "满意度": m7, "信用": m8, "在网": m9,
    }
    reason = np.full(n, "常规维护建议", dtype=object)
    n_map = len(_REASON_MAP)
    n_mask = len(_kw_to_mask)
    if n_map != n_mask:
        raise RuntimeError(
            f"_REASON_MAP({n_map}) 与掩码表({n_mask}) 长度不一致 —— "
            f"有人改了 _REASON_MAP 但没同步 _kw_to_mask，向量化理由会算错"
        )
    for keywords, text in reversed(_REASON_MAP):
        hit = _kw_to_mask.get(keywords[0])
        if hit is None:
            raise RuntimeError(
                f"_REASON_MAP 含未登记的关键词 {keywords!r}，向量化无法处理"
            )
        reason[hit] = text

    # ── 渠道与动作：查表，不做逐行分支 ─────────────────
    # 渠道由价值层硬定；动作由 (价值层, 风险等级) 决定 —— 与 recommend_action 同表
    channel = np.array([CHANNEL_BY_TIER["ZERO"], CHANNEL_BY_TIER["LOW"],
                        CHANNEL_BY_TIER["HIGH"]], dtype=object)[
        np.where(tier == "ZERO", 0, np.where(tier == "LOW", 1, 2))
    ]
    action = np.empty(n, dtype=object)
    for t in ("HIGH", "LOW", "ZERO"):
        for l in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            sel = (tier == t) & (lvl == l)
            if sel.any():
                action[sel] = _ACTION_TABLE[t][l]

    # ── 物化 dict（这一步占向量化后总时间的 ~90%，省不掉）──
    # 用 zip 拼接，比逐字段 dict 字面量快；key 顺序由 _SCORED_KEYS 保证
    ids = df["id"].tolist()
    cids = [str(x) for x in df["customer_id"].to_numpy()]
    surnames = [str(x) for x in df["surname"].to_numpy()]
    geos = [str(x) for x in df["geography"].to_numpy()]
    genders = [str(x) for x in df["gender"].to_numpy()]
    ages = age.tolist()
    tenures = tenure.tolist()
    bals = np.round(bal, 2).tolist()
    nprods = nprod.tolist()
    actives = active.tolist()
    credits = credit.tolist()
    # estimated_salary 同样是「csv 读进来的列」，这里不做 float32→64 假设，
    # 显式 astype 以与旧实现的 round(float(x), 2) 对齐
    salaries = np.round(df["estimated_salary"].to_numpy().astype(np.float64), 2).tolist()
    sats = sat.tolist()
    exited = df["exited"].to_numpy().tolist()
    probs = np.round(p, 4).tolist()
    lvls = lvl.tolist()
    tiers = tier.tolist()
    evs = ev.tolist()
    chans = channel.tolist()
    acts = action.tolist()
    reasons = reason.tolist()

    result = []
    for i in range(n):
        f = []
        if m1[i]:
            f.append("有投诉记录")
        if m2[i]:
            f.append("非活跃用户")
        if m3[i]:
            f.append(f"持有 {nprod[i]} 个产品（产品超载）")
        if m4[i]:
            f.append(f"高龄客户（{age[i]} 岁）")
        if m5[i]:
            f.append("账户余额为零")
        if m6[i]:
            f.append("德国地区客户")
        if m7[i]:
            f.append(f"满意度偏低（{sat[i]}/5）")
        if m8[i]:
            f.append(f"信用评分偏低（{credit[i]} 分）")
        if m9[i]:
            f.append(f"在网时长较短（{tenure[i]} 年）")
        result.append(dict(zip(_SCORED_KEYS, (
            ids[i], cids[i], surnames[i], geos[i], genders[i], ages[i],
            int(tenures[i]) if tenures[i] is not None else 0,
            float(bals[i]), int(nprods[i]), int(actives[i]), int(credits[i]),
            float(salaries[i]), int(sats[i]), int(exited[i]), float(probs[i]),
            str(lvls[i]), str(tiers[i]), float(evs[i]), str(chans[i]),
            str(acts[i]), str(acts[i]), str(reasons[i]), f,
        ))))
    return result


def get_scored_customers(db: Session):
    """对全量客户打分 + 分位数分级 + 风险因素/策略，结果缓存。返回 list[dict] 或 None。"""
    global _scored_cache

    engine = _ensure_engine(db)
    if engine is None:
        return None

    name = engine["name"]
    if _scored_cache["data"] is not None and _scored_cache["name"] == name \
            and time.time() - _scored_cache["ts"] < CACHE_TTL:
        return _scored_cache["data"]

    # 向量化构建（原逐行 df.iloc 循环改为数组运算，输出逐字段一致）
    result = _build_scored_result(engine["df"], engine["raw"], engine["thresholds"])

    _scored_cache = {"data": result, "name": name, "ts": time.time()}
    return result


# ── 单条预测（供 RiskPrediction 页复用）─────────────────

def classify_single(db: Session, raw_prob: float):
    """单条原始概率 → (概率, 风险等级)。"""
    engine = _ensure_engine(db)
    if engine is None:
        return round(float(raw_prob), 4), _legacy_level(raw_prob)
    p = float(raw_prob)
    return round(p, 4), _level(p, engine["thresholds"])


# ── 批量（供 batch_score / cost_benefit 复用）────────────

def calibrate_probs(db: Session, raw_probs):
    """批量：原始概率数组 → (概率数组, 风险等级列表)。不做校准，仅按分位数分级。"""
    engine = _ensure_engine(db)
    arr = np.asarray(raw_probs, dtype=float)
    if engine is None:
        return arr, [_legacy_level(float(p)) for p in arr]
    levels = [_level(float(p), engine["thresholds"]) for p in arr]
    return arr, levels


# ── 阈值信息（供接口/前端展示）─────────────────────────

def get_risk_info() -> dict:
    """当前风险分级标准信息。"""
    if _engine_cache["raw"] is None:
        return {
            "calibrated": False,
            "model": None,
            "cost_ratio": COST_RATIO,
            "optimal_threshold": None,
            "thresholds": {"critical": 0.7, "high": 0.3, "medium": 0.1},
        }
    return {
        "calibrated": True,
        "model": _engine_cache["name"],
        "cost_ratio": COST_RATIO,
        "optimal_threshold": round(_engine_cache["optimal_threshold"], 4),
        "thresholds": {k: round(v, 4) for k, v in _engine_cache["thresholds"].items()},
    }
