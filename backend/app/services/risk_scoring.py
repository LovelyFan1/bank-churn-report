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
"""

import json
import time
from pathlib import Path

import numpy as np
from sqlalchemy.orm import Session

from app.config import settings
from app.services.data_loader import DataLoader, prepare_features

MODEL_DIR = Path(__file__).parent.parent.parent / "saved_models"

# 漏检成本 / 误报成本 比值（与 cost_benefit 默认一致）
COST_RATIO = 5.0

# 分位数边界（百分位）
P_CRITICAL = 95   # top 5%
P_HIGH = 70       # top 30%
P_MEDIUM = 35     # top 65%

CACHE_TTL = 600  # 秒

# ── 进程级缓存 ───────────────────────────────────────────
_best_model_cache = {"name": None, "model": None}
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

def _load_best_model():
    """加载 AUC 最高的模型，进程级缓存。返回 (model, name)。"""
    if _best_model_cache["model"] is not None:
        return _best_model_cache["model"], _best_model_cache["name"]

    import joblib

    meta_path = MODEL_DIR / "meta.json"
    if not meta_path.exists():
        return None, None

    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)

    best_name = max(meta["results"].keys(), key=lambda x: meta["results"][x]["auc"])
    model_path = MODEL_DIR / (best_name.lower().replace(" ", "_") + ".joblib")
    if not model_path.exists():
        return None, None

    model = joblib.load(model_path)
    _best_model_cache["model"] = model
    _best_model_cache["name"] = best_name
    return model, best_name


# ── 成本收益最优阈值（参考）─────────────────────────────

def _optimal_threshold(raw_proba: np.ndarray, y_true: np.ndarray) -> float:
    """遍历阈值，返回净利润最大化的二分类阈值。"""
    best_t, best_profit = 0.5, -float("inf")
    for t in np.arange(0.05, 0.96, 0.05):
        pred = (raw_proba >= t).astype(int)
        tp = int(np.sum((pred == 1) & (y_true == 1)))
        fp = int(np.sum((pred == 1) & (y_true == 0)))
        fn = int(np.sum((pred == 0) & (y_true == 1)))
        profit = tp - fn * COST_RATIO - fp
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

    loader = DataLoader(db)
    df = loader.load_all()
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


# ── 风险因素 + 策略（规则，供列表/工单展示）──────────────

FACTOR_STRATEGY_MAP = [
    (["投诉"], "专属客户经理一对一挽留"),
    (["产品"], "产品整合推荐"),
    (["余额"], "专属费率激活"),
    (["活跃", "非活跃"], "主动外呼关怀"),
    (["满意度"], "满意度回访"),
    (["信用"], "定制化产品优惠方案"),
    (["在网"], "VIP费率优惠"),
    (["高龄", "年龄"], "定期回访关怀"),
    (["德国", "地区"], "定制化挽留方案"),
    (["积分"], "积分奖励计划"),
]


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


def _recommend_strategy(factors: list) -> str:
    if not factors:
        return "满意度回访"
    for keywords, strategy in FACTOR_STRATEGY_MAP:
        for f in factors:
            for kw in keywords:
                if kw in f:
                    return strategy
    return "满意度回访"


# ── 全量打分（缓存）─────────────────────────────────────

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

    df = engine["df"]
    raw = engine["raw"]
    thresholds = engine["thresholds"]

    result = []
    for i in range(len(df)):
        row = df.iloc[i]
        p = float(raw[i])
        factors = _risk_factors(row)
        tenure = row["tenure"]
        result.append({
            "id": int(row["id"]),
            "customer_id": str(row["customer_id"]),
            "surname": str(row["surname"]),
            "geography": str(row["geography"]),
            "gender": str(row["gender"]),
            "age": int(row["age"]),
            "tenure": int(tenure) if tenure is not None else 0,
            "balance": round(float(row["balance"]), 2),
            "num_products": int(row["num_products"]),
            "is_active_member": int(row["is_active_member"]),
            "credit_score": int(row["credit_score"]),
            "estimated_salary": round(float(row["estimated_salary"]), 2),
            "satisfaction_score": int(row["satisfaction_score"]),
            "exited": int(row["exited"]),
            "probability": round(p, 4),
            "risk_level": _level(p, thresholds),
            "risk_factors": factors,
            "strategy": _recommend_strategy(factors),
        })

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
