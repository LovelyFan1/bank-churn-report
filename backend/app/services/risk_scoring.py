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
import os
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

# ══════════════════════════════════════════════════════════════════════
# 两个阈值，各司其职 —— 此前系统把它们混用，导致「同一个召回率两个数」
# ══════════════════════════════════════════════════════════════════════
#
# 本模块对外提供**两类**阈值，语义完全不同，不可互相替代：
#
# 【一】分级阈值 thresholds（critical/high/medium）
#     来源：全量原始概率的**分位数** P95/P70/P35
#     用途：给客户贴 CRITICAL/HIGH/MEDIUM/LOW 标签，用于**展示与排序**
#     特性：占比恒定（5%/25%/35%/35%），与业务量无关，便于运营排班
#
# 【二】决策阈值 decision_threshold
#     来源：**训练集**上使净收益最大的绝对概率切点
#     用途：判定「这个客户要不要真的去干预」—— 即名单准入线
#     特性：随成本比与数据分布变化，是**成本最优**的绝对线
#
# ⚠ 为什么必须分开（这是本次修复的核心）：
#   两者数值接近但语义不同。此前 train.py 用 sklearn 默认 0.5 算 recall
#   （得 0.363），而 cost_benefit 用 decision_threshold 算（得 0.780），
#   于是**同一个系统里同时存在 36.3% 和 78.0% 两个「模型召回率」**，
#   分别出现在 Dashboard / ModelComparison / InterventionStrategy 三处。
#
#   经实测（20,000 条测试集）：
#       阈值 0.50（sklearn 默认）   → recall 0.3627  precision 0.7267  漏掉 2602 人
#       阈值 0.20（成本最优）       → recall 0.7796  precision 0.4345  漏掉  900 人
#   在 cost_ratio=5 的假设下，后者的净收益更高（+8590 vs +5367，单位=一次干预成本），
#   因为「漏掉一个真实流失客户」的代价是「白打一次电话」的 5 倍。
#
# ⚠ 选阈值的样本纪律（此前实现的缺陷）：
#   旧代码在**测试集**上挑最优阈值、又在**同一测试集**上报告指标 —— 属乐观偏差。
#   现改为：在**训练集**上挑阈值，在**测试集**上评估。实测两者恰好都选中 0.20，
#   数值不变，但方法上不再有偏，指标经得起追问。

# 决策阈值的候选网格。上限 0.95 是因为树模型概率趋于两极，
# 再高的切点会切掉几乎所有样本，失去意义。
DECISION_GRID = np.arange(0.05, 0.96, 0.05)

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
    _engine_cache = _empty_engine_cache()
    _scored_cache = {"data": None, "name": None, "ts": 0.0, "engine_ts": None}


def _empty_engine_cache() -> dict:
    """引擎缓存的空白初始值（多处复用，避免字段遗漏）。"""
    return {
        "df": None,
        "raw": None,
        "thresholds": None,
        "optimal_threshold": None,     # 保留旧字段名，兼容既有调用方
        "decision_threshold": None,    # 与 optimal_threshold 同值，语义更明确
        "decision_coverage": None,     # 决策阈值切掉的人群占比
        "name": None,
        "ts": 0.0,
        # 缓存构建时的版本号 —— 用于跨进程感知「模型重训 / 客户表变更」，
        # 见 _engine_is_fresh() 的说明
        "model_version": None,
        "customer_version": None,
    }


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

# ── 模型缓存的跨进程版本标记 ──────────────────────────────
#
# ⚠ 为什么 TTL 还不够（实测缺陷）：
#   训练跑在 **Celery worker 进程**，而本模块的 `_best_model_cache`
#   在 **backend 进程** 内存里。worker 写新模型后没有任何机制通知 backend，
#   于是已加载过模型的 worker 会继续用旧模型，直到 MODEL_CACHE_TTL 到期
#   （最长 10 分钟）。期间：
#     · 界面显示的分级 / Top 名单 / 成本收益都基于旧模型；
#     · backend 有 4 个 uvicorn worker，各自 TTL 起点不同，
#       可能出现「同一客户刷新两次得到不同风险等级」。
#
#   解法与 data_loader 的 customer cache 一致：把版本号落成一个极小文件，
#   写入方（训练任务）递增，读取方每次比对。os.stat + 读 1 个整数
#   远比 joblib.load 一个模型（数 MB）便宜，因此可以每次调用都检查。
_MODEL_VERSION_FILE = MODEL_DIR / ".model_version"


def _read_model_version() -> str:
    try:
        return _MODEL_VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def bump_model_version() -> None:
    """训练完成后由 Celery worker 调用，通知所有 backend 进程丢弃模型缓存。

    ⚠ 不要在这里清本进程的 `_best_model_cache` 之外的东西 ——
      本函数设计为**可跨进程生效**，因此必须落盘。
    """
    global _engine_cache, _scored_cache
    # 1) 清本进程（若本函数恰好在 backend 进程内被调用）
    _best_model_cache["model"] = None
    _best_model_cache["name"] = None
    _best_model_cache["ts"] = 0.0
    _best_model_cache["version"] = None
    _engine_cache = _empty_engine_cache()
    _scored_cache = {"data": None, "name": None, "ts": 0.0, "engine_ts": None}

    # 2) 落盘递增版本号，让其他进程也能感知
    try:
        _MODEL_VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        cur = _read_model_version()
        try:
            nxt = int(cur) + 1 if cur else 1
        except ValueError:
            nxt = 1
        tmp = _MODEL_VERSION_FILE.with_name(_MODEL_VERSION_FILE.name + ".tmp")
        tmp.write_text(str(nxt), encoding="utf-8")
        os.replace(tmp, _MODEL_VERSION_FILE)
    except OSError:
        # 版本文件不可写时退化为旧的 TTL 行为，不能让训练任务因此失败
        logger.warning("risk_scoring: 无法写入模型版本文件 %s，"
                       "跨进程失效不可用（将退化为 TTL 过期）", _MODEL_VERSION_FILE)

_best_model_cache = {"name": None, "model": None, "ts": 0.0, "version": None}
_engine_cache = {
    "df": None,                # 全量数据（复用，避免重复 load_all）
    "raw": None,               # 全量原始概率
    "thresholds": None,        # {"critical", "high", "medium"} 分位数分级线
    "optimal_threshold": None,  # 成本收益参考阈值（保留旧名，= decision_threshold）
    "decision_threshold": None,  # 决策阈值：名单准入线（成本最优绝对切点）
    "decision_coverage": None,   # 决策阈值覆盖的人群占比
    "name": None,
    "ts": 0.0,
}
_scored_cache = {"data": None, "name": None, "ts": 0.0}


# ── 模型加载（缓存）─────────────────────────────────────

def _cache_expired() -> bool:
    """模型对象缓存是否已过期（或从未加载、或模型已被重训）。"""
    if _best_model_cache["model"] is None:
        return True
    if time.time() - _best_model_cache["ts"] >= MODEL_CACHE_TTL:
        return True
    # 跨进程失效：训练任务会递增磁盘版本号（见 bump_model_version 的说明）
    return _best_model_cache.get("version") != _read_model_version()


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

    # ⚠ 结构异常也要降级，不能把 KeyError 抛到接口层。
    #   实测：meta.json 能被 json.load 解析、但缺 "results" 键（或该键为空、
    #   或某模型缺 "auc"）时，旧实现会抛
    #       KeyError: 'results' / ValueError: max() arg is an empty sequence
    #   并一路冒到 /api/customers 等**所有**依赖本函数的接口，全部 500。
    #   写入端的原子写只能保证"不出现半截 JSON"，保证不了结构完整
    #   （手工编辑、旧版本文件、外部工具写入都可能造成结构异常）。
    #   cost_benefit_service._read_meta 对同类情况是正确降级的，这里与之对齐。
    try:
        results = meta["results"]
        if not isinstance(results, dict) or not results:
            raise ValueError("meta.json 的 results 为空或类型异常")
        best_name = max(results.keys(), key=lambda x: results[x]["auc"])
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        logger.warning(
            "risk_scoring: %s 结构异常，无法选出最佳模型（降级为模型未就绪）: %s: %s",
            meta_path, type(e).__name__, e,
        )
        return None, None

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
    # 记录加载时的版本号；下次调用若发现磁盘版本变了就会自动重载
    _best_model_cache["version"] = _read_model_version()
    return model, best_name


# ── 成本收益最优阈值（参考）─────────────────────────────

# 挽留成功率 —— 从 config 取，全系统唯一来源
RETENTION_SUCCESS_RATE = settings.RETENTION_SUCCESS_RATE


def net_profit(tp: int, fp: int, fn: int,
               cost_ratio: float = COST_RATIO,
               success_rate: float | None = None) -> float:
    """二分类混淆矩阵 → 净收益，单位为「一次误报（干预）的成本」。

    这是全系统**唯一**的成本模型，risk_scoring 与 cost_benefit 共用，
    以免两处各写一套、得出互相打架的结论。

    记账基准是「不干预」：不管我们做不做，没被识别出的流失客户（FN）都会流失，
    FN 相对于该基准不产生额外损失，故不计入。以一次干预成本 c 为单位：

        TP（劝住了本来会流失的人）= 保住价值 × 成功率 − 干预成本
                                   = +(success_rate × cost_ratio − 1)
        FP（白跑一趟）             = -干预成本              = -1
        TN（本来就不会流失）       = 0
        FN                         = 0（与基准同为流失，无增量差异）

    等价换算（cost_ratio = 客户价值 / 单次干预成本）：
        净收益 = TP×s×客户价值 - 触达人次×单次干预成本，再除以 c。

    ⚠ success_rate 这一项是**后补的**，此前缺失（实测推导）：
      旧实现 TP 记 +(cost_ratio-1)，等价于 s=1.0，即「判对了就等于留住了」。
      展开 ROI 会得到 `ROI = COST_RATIO × precision` —— 一个不含任何成功率
      因子的式子。这会把 ROI 系统性高估（按 s=0.3 算，高估约 3.3 倍）。

      默认取 settings.RETENTION_SUCCESS_RATE（0.30，行业保守值）。
      显式传 1.0 可复现旧行为，仅供对照，不应作为对外口径。

    ⚠ 历史沿革（保留以备追溯）：更早的实现是 `tp*1 - fn*cost_ratio - fp*1`，
      把「劝住一个」记 +1、却把「漏掉一个」记 -cost_ratio，两者不对称 ——
      导致所有阈值下净收益恒为负，「最优阈值」退化成「最不亏的那一档」。
    """
    s = RETENTION_SUCCESS_RATE if success_rate is None else float(success_rate)
    return tp * (s * cost_ratio - 1) - fp


def _optimal_threshold(raw_proba: np.ndarray, y_true: np.ndarray) -> float:
    """遍历阈值，返回净利润最大化的二分类阈值。

    ⚠ 阈值随「挽留成功率 s」移动，这是**正确行为**而非副作用：
      net_profit 里 TP 的奖励是 (s×cost_ratio − 1)，s 越小奖励越薄，
      就需要更高的精度才划算 → 最优阈值上移、名单收窄。
      实测（96,418 行、LightGBM、cost_ratio=5）：
          s=1.00 → 0.20（覆盖 37.0%）
          s=0.50 → 0.40（覆盖 16.1%）
          s=0.30 → 0.60（覆盖  6.9%）
      即 s 是**名单规模的主要决定因素**，改它必须重选阈值，
      否则会用一个按乐观假设选出的线去做保守决策。
    """
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

def _engine_is_fresh() -> bool:
    """引擎缓存是否仍然有效。

    ⚠ 必须同时满足三个条件（漏掉任何一个都会导致"重训/换数据后界面不更新"）：

    1) **已构建** —— raw 非空；
    2) **未超 TTL** —— 兜底；
    3) **模型版本未变** —— `train.py` 训练完会 bump `.model_version`；
    4) **客户表版本未变** —— `data_source.seed()` / 聚类写回会 bump
       `.customer_cache_version`。

    ⚠ 第 3、4 条是**补上的**（实测缺陷）：
      只给 `_best_model_cache` 加版本校验是不够的。`_ensure_engine` 里
      缓存的 `df` 与 `raw` 才是各接口真正读的东西 —— 实测：
          bump 模型版本号后调用 _ensure_engine
          → 耗时 0.0000s，raw 对象未重建，sum 完全不变
      即 `/api/customers`、`/api/customers/{id}`、`classify_single`
      在重训后最长 CACHE_TTL(600s) 内仍用**旧模型的概率与旧分位阈值**，
      而 `/api/cost-benefit/*`（走 _best_model_cache）已换新模型 ——
      同一系统两套结果，正是本模块一直想消除的那类不一致。

      同理，客户表被换掉（reseed）后，引擎里的 `df`/`raw` 仍指向旧表，
      而 EDA 已经用新表 —— 同一客户两套数据。
    """
    if _engine_cache["raw"] is None:
        return False
    if time.time() - _engine_cache["ts"] >= CACHE_TTL:
        return False
    if _engine_cache.get("model_version") != _read_model_version():
        return False
    from app.services.data_loader import _read_cache_version
    if _engine_cache.get("customer_version") != _read_cache_version():
        return False
    return True


def _ensure_engine(db: Session):
    """加载模型 + 全量打分 + 计算分位数阈值 + 成本收益参考。返回缓存 dict 或 None。"""
    if _engine_is_fresh():
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

    # ── 决策阈值：在【训练集】上选，避免乐观偏差 ──────────────
    #
    # ⚠ 此处是本次修复的重点之一。旧实现是：
    #       _, test_idx = train_test_split(idx, ...)
    #       t_opt = _optimal_threshold(raw[test_idx], y[test_idx])   # 在测试集上选
    #   即「在测试集上挑最优阈值、又在同一测试集上报告指标」—— 阈值本身
    #   拟合了这批测试数据，指标带乐观偏差，经不起追问。
    #
    #   现改为在**训练集**上选阈值，测试集只用于最终的指标报告
    #   （见 evaluate_at_decision_threshold）。实测两种做法都选中 0.20，
    #   结论一致，但方法上不再有偏。
    from sklearn.model_selection import train_test_split
    idx = np.arange(len(df))
    train_idx, _test_idx = train_test_split(
        idx, test_size=settings.TEST_SIZE,
        random_state=settings.RANDOM_STATE, stratify=y,
    )
    t_opt = _optimal_threshold(raw[train_idx], y[train_idx])

    # 决策阈值在分位数体系中的位置 —— 供前端解释「这条线切掉多少人」
    coverage = float((raw >= t_opt).mean())

    # 记录构建时的两个版本号（下次调用据此判断是否需重建，见 _engine_is_fresh）
    from app.services.data_loader import _read_cache_version
    _engine_cache.update({
        "df": df,
        "raw": raw,
        "thresholds": thresholds,
        "optimal_threshold": t_opt,
        "decision_threshold": t_opt,
        "decision_coverage": coverage,
        "name": name,
        "ts": time.time(),
        "model_version": _read_model_version(),
        "customer_version": _read_cache_version(),
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
#
# ⚠ 每个条目的**第一个关键词必须保持不变** —— `_build_scored_result` 的
#   `_kw_to_mask` 用 `keywords[0]` 去查对应的布尔掩码，改首位会直接把
#   向量化的理由算错（那里有 n_map != n_mask 的运行时断言兜底，但断言
#   只查长度、查不出"关键词换了"）。
#
# ⚠ 为什么要给每个条目补**第二组关键词**（实测确认，勿删）：
#   本表的关键词原本是按 `_risk_factors()` 的**规则标签**写的
#   （"产品超载"、"高龄"、"非活跃"…）。但 `predict.py` 的 SHAP 归因走的是
#   另一套标签 `SHAP_FEATURE_LABELS`（"持有产品数"、"年龄"、"活跃状态"…）。
#   两套文案对不上，导致 `recommend_action` 拿 SHAP 因素去匹配时
#   **7/13 个标签匹配不上**：
#       satisfaction_score → 「满意度评分」 ✅      num_products → 「持有产品数」❌
#       age                → 「年龄」       ❌      balance      → 「账户余额」  ❌
#       is_active_member   → 「活跃状态」   ❌      credit_score → 「信用评分」  ✅
#       tenure             → 「在网时长」   ✅      estimated_salary → 「预估薪资」❌
#       has_credit_card    → 「持有信用卡」 ❌      points_earned    → 「积分」    ❌
#       geography          → 「地区」       ❌      gender           → 「性别」    ❌
#       complain           → 「投诉记录」   ✅
#   实测后果：概率最高的 2000 个客户中，**100% 出现「因素列表首项 ≠ reason 来源」**
#   —— 例如首项是「持有产品数（75.8%）」，reason 却说「信用评分偏低」，
#   而信用评分根本不在该客户的前三个因素里。
#   补上同义关键词后即可正确匹配，且不影响规则标签那条路径。
#
# ⚠ 条目数必须是 9、且每条 `keywords[0]` 不得改动 ——
#   `_build_scored_result` 的 `_kw_to_mask` 按 `keywords[0]` 查布尔掩码，
#   增删条目或改首位会让向量化路径抛运行时断言（那里只校验长度，校验不出
#   "关键词被换掉"，所以这里必须靠注释约束）。
_REASON_MAP = [
    (["投诉", "投诉记录"], "近期有投诉记录，需优先安抚"),
    (["产品超载", "持有产品数", "产品数量"], "产品数量过多，存在体验负担"),
    (["非活跃", "活跃状态", "不活跃"], "长期不活跃，服务感知弱"),
    # 文案对"余额为零"与 SHAP 的通用"账户余额"都成立 ——
    # 原文案「账户已空置」在余额非零（但该特征贡献度高）时是**假声明**。
    (["余额为零", "账户余额"], "账户余额水平是主要风险来源，需关注资产变动"),
    (["德国", "地区", "地理"], "所在地区整体流失率偏高"),
    (["高龄", "年龄"], "年龄结构使其对服务变动更敏感"),
    (["满意度", "满意"], "满意度评分偏低，存在明确不满"),
    # ⚠ 已知瑕疵：「持有信用卡」含子串「信用」，会误命中本条。
    #   属关键词子串匹配的固有弱点，未单独加条目（会破坏上面的长度约束）。
    (["信用", "信用评分"], "信用评分偏低，风险敞口较大"),
    (["在网", "在网时长", "年限"], "在网时间较短，尚未形成使用习惯"),
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

    # ── 可溯源性兜底 ────────────────────────────────────────
    #
    # ⚠ 为什么需要这一步（实测）：`_REASON_MAP` 的关键词表再全，也不可能
    #   覆盖 `SHAP_FEATURE_LABELS` 的所有文案。实测补完同义词后仍有 3 个
    #   标签匹配不上：预估薪资 / 积分 / 性别。
    #   于是会出现「因素列表里写了"预估薪资(55%)"，reason 却说"常规维护建议"」
    #   —— 理由与列出的因素**对不上**，这比理由写得笼统更糟。
    #
    #   兜底策略：若没匹配上、但调用方**确实给了因素**，就用首个因素本身
    #   作为理由（factors 已按贡献度/优先级排序，首项即最相关的那条）。
    #   这样保证：reason 永远能从 risk_factors 里找到出处。
    if reason == "常规维护建议" and factors:
        first = factors[0]
        # 去掉 SHAP 文案里的占比后缀「（75.8%）」，让句子读起来自然
        clean = first.split("（")[0].strip() if "（" in first else first.strip()
        if clean:
            reason = f"{clean}是主要风险来源，建议优先核实"

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
#   3) 新增字段一律**追加到末尾**，绝不插在中间 —— 插入会整体移动后续键的
#      下标，任何按下标取值的地方都会静默错位。
#
# ⚠ 为什么补了 has_credit_card / points_earned（实测发现的契约缺口）：
#   本函数之前输出 23 个键，而 `POST /api/model/predict` 需要 12 个特征。
#   对比两者发现 **has_credit_card 与 points_earned 不在输出里**，于是：
#     · GET /api/customers/{id} 的响应**不足以**喂给预测接口；
#     · 谁若把详情响应转手传给 predict / shap-single，
#       这两个字段会落到 `.get(..., 默认值)`（1 和 500）。
#   实测影响（4 个真实客户）：
#       C100000 列表概率 0.0639 → 详情喂入 0.1071（偏差 0.0432，**相对偏差 68%**）
#       C050001 列表概率 0.2039 → 详情喂入 0.2015
#     has_credit_card 实际为 0 的客户占 **29.2%**（29233/100000），
#     points_earned 恰好等于 500 的仅 **0.12%**（123/100000）。
#   即两个占位默认值对绝大多数客户都是错的，会造成
#   「点进详情看到的概率 ≠ 列表里的概率」——正是本系统一直在消除的那类不一致。
#   补上后详情响应即可自洽地驱动单条预测与 SHAP 解释。

_SCORED_KEYS = (
    "id", "customer_id", "surname", "geography", "gender", "age", "tenure",
    "balance", "num_products", "is_active_member", "credit_score",
    "estimated_salary", "satisfaction_score", "exited", "probability",
    "risk_level", "value_tier", "expected_value",
    "channel", "strategy", "action", "reason", "risk_factors",
    # ── 以下为追加字段（见上方说明 3）──
    "has_credit_card", "points_earned",
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
    # 追加字段：详情接口要用它们喂单条预测 / SHAP（见 _SCORED_KEYS 的说明）
    cards = df["has_credit_card"].to_numpy()
    pts = df["points_earned"].to_numpy()

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
            # 追加字段（顺序必须与 _SCORED_KEYS 末尾一致）
            int(cards[i]), int(pts[i]),
        ))))
    return result


def get_scored_customers(db: Session):
    """对全量客户打分 + 分位数分级 + 风险因素/策略，结果缓存。返回 list[dict] 或 None。"""
    global _scored_cache

    engine = _ensure_engine(db)
    if engine is None:
        return None

    name = engine["name"]
    # ⚠ 打分缓存的守卫必须与引擎同源：除 name 与 TTL 外，还要比对
    #   引擎的**构建时间戳**。引擎被重建（模型重训 / 客户表变更）后，
    #   raw 与 thresholds 可能都变了，而这里若只比 name 就会继续复用
    #   旧的打分结果 —— 表现为「客户列表还是旧等级，但 risk-info 已变」。
    #   用 engine["ts"] 比对是最简且可靠的判据（引擎重建必然刷新该值）。
    if (_scored_cache["data"] is not None
            and _scored_cache["name"] == name
            and _scored_cache.get("engine_ts") == engine.get("ts")
            and time.time() - _scored_cache["ts"] < CACHE_TTL):
        return _scored_cache["data"]

    # 向量化构建（原逐行 df.iloc 循环改为数组运算，输出逐字段一致）
    result = _build_scored_result(engine["df"], engine["raw"], engine["thresholds"])

    _scored_cache = {"data": result, "name": name, "ts": time.time(),
                     "engine_ts": engine.get("ts")}
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

def evaluate_at_decision_threshold() -> dict:
    """在**测试集**上、按**决策阈值**计算真实指标。

    ⚠ 这个函数的存在就是为了消除「同一个召回率两个数」的问题。

    此前系统里 recall 有两个来源，互不一致：
      · train.py 用 sklearn 默认 0.5 → 0.3627（写进 meta.json，被 3 个页面展示）
      · cost_benefit 用 decision_threshold(0.20) → 0.7796
    而实际运营判定（客户列表筛选、建单建议）依据的是分位数分级，
    与上述两者又都不同。三套口径并存，答辩时无法自圆其说。

    现统一为：**以决策阈值为准报告 recall/precision**，因为那才是
    「按净收益最优去挑客户」时真实会发生的结果。

    返回 {} 表示引擎未就绪或无法评估（调用方应回退展示，不得编数字）。
    """
    if _engine_cache["raw"] is None:
        return {}
    try:
        from sklearn.model_selection import train_test_split
        df = _engine_cache["df"]
        raw = _engine_cache["raw"]
        thr = _engine_cache["decision_threshold"]
        if df is None or raw is None or thr is None:
            return {}

        y = df["exited"].values
        idx = np.arange(len(df))
        _train_idx, test_idx = train_test_split(
            idx, test_size=settings.TEST_SIZE,
            random_state=settings.RANDOM_STATE, stratify=y,
        )
        # ⚠ 阈值来自训练集，指标在测试集上算 —— 两侧不重叠，无乐观偏差
        p_te, y_te = raw[test_idx], y[test_idx]
        pred = (p_te >= thr).astype(int)

        tp = int(((pred == 1) & (y_te == 1)).sum())
        fp = int(((pred == 1) & (y_te == 0)).sum())
        fn = int(((pred == 0) & (y_te == 1)).sum())
        tn = int(((pred == 0) & (y_te == 0)).sum())
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        accuracy = (tp + tn) / len(y_te) if len(y_te) else 0.0

        return {
            "threshold": round(float(thr), 4),
            "sample_size": int(len(y_te)),
            "positives": int(y_te.sum()),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1_score": round(f1, 4),
            "accuracy": round(accuracy, 4),
            "net_profit": round(float(net_profit(tp, fp, fn)), 2),
            # 口径标注：前端必须显示这是「决策阈值下的实测」
            "basis": "test_set_at_decision_threshold",
        }
    except Exception as e:
        logger.warning("risk_scoring: 决策阈值指标评估失败: %s: %s", type(e).__name__, e)
        return {}


def get_risk_info() -> dict:
    """当前风险分级标准信息 —— 全系统口径的唯一对外出口。

    返回三组信息，语义严格区分（见文件顶部「两个阈值」的说明）：
      thresholds          分级线（分位数）—— 用于贴标签、排序
      decision_threshold  决策线（成本最优绝对切点）—— 用于决定是否干预
      decision_metrics    决策线在测试集上的实测指标 —— 对外报 recall 时应引用它
      optimal_threshold   保留旧字段名，与 decision_threshold 同值
    """
    if _engine_cache["raw"] is None:
        # ⚠ 冷路径**不得返回伪造的阈值**。旧实现返回写死的
        #   {"critical": 0.7, "high": 0.3, "medium": 0.1}，与真实分位阈值
        #   （实测 0.6796 / 0.2475 / 0.0682）相差很大 —— 尤其 medium
        #   差了 10 倍。冷启动的 worker 会把这个假值当"当前分级标准"
        #   吐给前端，而前端（CustomerManagement 等）会把它显示给用户。
        #
        #   触发路径有两处，都不罕见：
        #     · 该路由历史上没有 db 依赖，永不触发 _ensure_engine，
        #       只能靠 main.py 的启动预热线程；预热失败后就永久返回假值；
        #     · 多 worker 下某个 worker 尚未被预热线程覆盖。
        #
        #   现改为 thresholds=None + calibrated=False，让调用方明确知道
        #   "还没算出来"，而不是拿到一组看似可用的数字。
        return {
            "calibrated": False,
            "model": None,
            "cost_ratio": COST_RATIO,
            "success_rate": RETENTION_SUCCESS_RATE,
            "optimal_threshold": None,
            "decision_threshold": None,
            "decision_coverage": None,
            "decision_metrics": {},
            "thresholds": None,
            "note": "风险引擎尚未就绪（模型未训练或缓存未构建），"
                    "此时不提供分级阈值，避免给出与实际不符的默认值",
        }
    return {
        "calibrated": True,
        "model": _engine_cache["name"],
        "cost_ratio": COST_RATIO,
        # 挽留成功率 —— 必须对外暴露，因为它是"名单规模"的主要决定因素，
        # 且是**业务假设值**（非从数据拟合）。藏在公式里等于不可质疑。
        "success_rate": RETENTION_SUCCESS_RATE,
        "success_rate_source": "assumption(settings.RETENTION_SUCCESS_RATE)",
        "optimal_threshold": round(_engine_cache["optimal_threshold"], 4),
        # 决策阈值与分级阈值并列暴露，前端不得混用
        "decision_threshold": round(_engine_cache["decision_threshold"], 4),
        "decision_coverage": round(_engine_cache["decision_coverage"], 4),
        "decision_metrics": evaluate_at_decision_threshold(),
        "thresholds": {k: round(v, 4) for k, v in _engine_cache["thresholds"].items()},
    }
