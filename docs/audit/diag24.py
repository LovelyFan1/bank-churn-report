"""第二十四轮：核实「最优阈值」在全系统是否真的唯一。

⚠ 疑点：两个模块各自"选"最优阈值，但用的样本不同：
     risk_scoring._ensure_engine : 在**全量 10 万行** raw 上选   → 实测 0.2
     cost_benefit.analyze_thresholds : 在**测试集 2 万行**上选   → 需实测
   若两者不等，则系统里仍有"两个最优阈值"。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np, json
from app.database import SessionLocal
from app.services import risk_scoring as rs
from app.services.cost_benefit_service import CostBenefitService

db = SessionLocal()

print("=" * 92)
print("一、cost_benefit.analyze_thresholds 自己选出的最优阈值")
print("=" * 92)
svc = CostBenefitService(db)
an = svc.analyze_thresholds()
if "error" in an:
    print(f"  ❌ {an['error']}")
else:
    opt = an["optimal_metrics"]
    print(f"  best_model        = {an['best_model']}")
    print(f"  test_size         = {an['test_size']}")
    print(f"  churn_count       = {an['churn_count']}")
    print(f"  optimal_threshold = {an['optimal_threshold']}")
    print(f"    recall={opt['recall']} precision={opt['precision']} "
          f"net_profit={opt['net_profit']} fn={opt['fn']} fp={opt['fp']}")

print()
print("=" * 92)
print("二、risk_scoring 引擎的 decision_threshold")
print("=" * 92)
eng = rs._ensure_engine(db)
print(f"  decision_threshold = {eng['decision_threshold']}")
print(f"  decision_coverage  = {eng['decision_coverage']}")

print()
print("=" * 92)
print("三、两者是否一致")
print("=" * 92)
if "error" not in an:
    same = abs(an["optimal_threshold"] - eng["decision_threshold"]) < 1e-9
    print(f"  cost_benefit optimal_threshold = {an['optimal_threshold']}")
    print(f"  risk_scoring decision_threshold= {eng['decision_threshold']}")
    print(f"  → {'✅ 一致' if same else '❌ 不一致！这就是第二个口径问题'}")

print()
print("=" * 92)
print("四、风险：cost_benefit 用测试集选阈值 = 乐观偏差（方法学）")
print("=" * 92)
from sklearn.model_selection import train_test_split
from app.config import settings
raw = eng["raw"]; df = eng["df"]; y = df["exited"].values
idx = np.arange(len(df))
tr, te = train_test_split(idx, test_size=settings.TEST_SIZE,
                          random_state=settings.RANDOM_STATE, stratify=y)
print(f"  risk_scoring  在**训练集**(48000... 实际 {len(tr)} 行)上选 → "
      f"{rs._optimal_threshold(raw[tr], y[tr])}")
print(f"  cost_benefit  在**测试集**({len(te)} 行)上选 → "
      f"{rs._optimal_threshold(raw[te], y[te])}")
print(f"  全量上选 → {rs._optimal_threshold(raw, y)}")

# 测试集上的净收益 vs 训练集选出的阈值在测试集上的净收益
def profit_at(t, p, yy):
    pred = (p >= t).astype(int)
    tp = int(((pred == 1) & (yy == 1)).sum())
    fp = int(((pred == 1) & (yy == 0)).sum())
    fn = int(((pred == 0) & (yy == 1)).sum())
    return rs.net_profit(tp, fp, fn)

t_test = rs._optimal_threshold(raw[te], y[te])
t_train = rs._optimal_threshold(raw[tr], y[tr])
p_te, y_te = raw[te], y[te]
print(f"\n  测试集上：")
print(f"    用 test-选出的 {t_test}: 净收益 = {profit_at(t_test, p_te, y_te):,.0f}")
print(f"    用 train-选出的 {t_train}: 净收益 = {profit_at(t_train, p_te, y_te):,.0f}")
print(f"    → 差异 {profit_at(t_test,p_te,y_te) - profit_at(t_train,p_te,y_te):,.0f}"
      f"（'测试集选阈值'天然占优，这就是乐观偏差的量化）")

print()
print("=" * 92)
print("五、Dashboard 上展示的 model_recall 来源追踪")
print("=" * 92)
bs = svc.get_business_summary()
print(f"  business_summary.model_recall    = {bs.get('model_recall')}")
print(f"  business_summary.model_precision = {bs.get('model_precision')}")
print(f"  business_summary.optimal_threshold = {bs.get('optimal_threshold')}")
ri = rs.get_risk_info()
print(f"  risk_info.decision_metrics.recall = {ri['decision_metrics'].get('recall')}")
print(f"  risk_info.decision_threshold      = {ri['decision_threshold']}")
print()
print("  ⚠ 两者算 recall 用的**样本**不同：")
print("     business_summary : 测试集（cost_benefit 口径）")
print("     risk_info        : 测试集（risk_scoring 口径）")
print("     但 threshold 来源不同（测试集挑 vs 全量挑）")

db.close()
