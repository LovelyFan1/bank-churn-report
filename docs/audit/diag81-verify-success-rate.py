"""验证「挽留成功率」修复 + 两个 ROI 口径分离。

断言清单：
  A) ROI 公式现在含成功率：ROI == COST_RATIO × precision × s
  B) s 进入阈值选择：阈值应随 s 移动（s=0.30 → 0.60）
  C) 两个 ROI 各自命名，字段不再撞名（roi vs roi_executed）
  D) 语义修正：retained_customers 现在等于 TP × s（不再是 TP）
  E) 兼容字段与新增字段自洽
  F) retention-summary 如实标注数据来源为播种演示数据
"""
import json
import urllib.request

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


fails = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(name)


print("=" * 78)
print("A) ROI 公式含成功率")
print("=" * 78)
ri = get("/api/model/risk-info")
bs = get("/api/dashboard/summary")["business_summary"]
dm = ri["decision_metrics"]

cr = ri["cost_ratio"]
s = ri["success_rate"]
prec = dm["precision"]
expect_roi = cr * prec * s

print(f"    cost_ratio={cr}  precision={prec}  success_rate={s}")
print(f"    公式值 = {cr} × {prec} × {s} = {expect_roi:.4f}")
print(f"    接口值 = {bs['roi']}")
check("ROI == CR × precision × s", abs(bs["roi"] - expect_roi) < 0.02,
      f"差 {abs(bs['roi']-expect_roi):.4f}")

print()
print("    对照：若按 s=1.0 计算（旧口径）")
old_roi = cr * prec * 1.0
print(f"      ROI(s=1) = {old_roi:.2f}   新 ROI = {bs['roi']}   "
      f"高估倍数 = {old_roi/bs['roi']:.2f}x")
check("新 ROI 确实低于旧口径", bs["roi"] < old_roi)

print()
print("=" * 78)
print("B) 成功率进入阈值选择")
print("=" * 78)
print(f"    decision_threshold = {ri['decision_threshold']}")
print(f"    decision_coverage  = {ri['decision_coverage']*100:.2f}%")
check("阈值已随 s=0.30 上移至 0.60", abs(ri["decision_threshold"] - 0.60) < 1e-6,
      f"实际 {ri['decision_threshold']}")
check("覆盖率随之收窄到 6.83%", abs(ri["decision_coverage"] - 0.0683) < 0.002,
      f"实际 {ri['decision_coverage']*100:.2f}%")

print()
print("    阈值从 0.20 → 0.60 的连带效果（测试集 19,284 人）：")
print(f"      触达人数   {int(19284*0.3695):>7,} → {int(19284*ri['decision_coverage']):>7,}")
print(f"      召回率     0.7811 → {dm['recall']}")
print(f"      精准率     0.4304 → {dm['precision']}")
print("      ⚠ 召回率下降是**预期结果**：成功率低时，只该去打扰最有把握的人。")

print()
print("=" * 78)
print("C) 两个 ROI 各自命名，不再撞名")
print("=" * 78)
rs = get("/api/cost-benefit/retention-summary")
check("retention 不再有 roi 字段（避免与推算 ROI 混用）", "roi" not in rs,
      f"实际键含 roi: {'roi' in rs}")
check("retention 有 roi_executed", "roi_executed" in rs,
      f"= {rs.get('roi_executed')}")
check("retention 有 roi_basis 标注", rs.get("roi_basis") == "executed_orders_only",
      f"= {rs.get('roi_basis')}")
check("retention 给同口径折算值", rs.get("roi_if_same_basis_as_plan") is not None,
      f"= {rs.get('roi_if_same_basis_as_plan')}")

print()
print("=" * 78)
print("D) 语义修正：retained_customers 不再是裸 TP")
print("=" * 78)
tp = bs["tp_at_threshold"]
er = bs["expected_retained"]
print(f"    tp_at_threshold   = {tp:,}   （模型判对人数，不含成功率）")
print(f"    expected_retained = {er:,}   = TP × s")
print(f"    retained_customers= {bs['retained_customers']:,}   （兼容字段，语义=期望值）")
check("retained_customers == expected_retained（旧字段已修正语义）",
      bs["retained_customers"] == er)
check("expected_retained == round(TP × s)", abs(er - tp * s) <= 1,
      f"{tp} × {s} = {tp*s:.0f} vs {er}")
check("expected_retained < tp（成功率<1 故必然更小）", er < tp)

print()
print("    金额侧同样修正：")
print(f"      expected_reduced_loss = {bs['expected_reduced_loss']:,}")
print(f"      reduced_loss(兼容)     = {bs['reduced_loss']:,}")
check("reduced_loss == expected_reduced_loss",
      bs["reduced_loss"] == bs["expected_reduced_loss"])
check("reduced_loss == expected_retained × 客单价",
      abs(bs["reduced_loss"] - er * bs["avg_customer_value"]) < 1)

print()
print("=" * 78)
print("E) 核心恒等式复算（对外数字的推导链）")
print("=" * 78)
flag = bs["annual_flagged"]
icost = bs["intervention_cost"]
print(f"    1) annual_flagged = churn × recall/precision")
recall = bs["model_recall"]
precision = bs["model_precision"]
calc = bs["annual_churn_count"] * recall / precision
check("触达人次可复算", abs(flag - calc) < 2, f"{calc:.0f} vs {flag}")

print(f"    2) intervention_cost = annual_flagged × (AVG/CR)")
calc_cost = flag * bs["cost_per_intervention"]
check("投入可复算", abs(icost - calc_cost) < 1, f"{calc_cost:,.0f} vs {icost:,.0f}")

print(f"    3) ROI = expected_reduced_loss / intervention_cost")
calc_roi = bs["expected_reduced_loss"] / icost
check("ROI 可复算", abs(calc_roi - bs["roi"]) < 0.02, f"{calc_roi:.4f} vs {bs['roi']}")

print(f"    4) annual_churn_count == 真实流失数（不再用 total×rate 取整）")
print(f"       = {bs['annual_churn_count']:,}")

print()
print("=" * 78)
print("G) 三接口阈值一致性（本次顺带修掉的隐藏缺陷）")
print("=" * 78)
cb = get("/api/cost-benefit/summary")
print(f"    risk-info      : thr={ri['decision_threshold']}  "
      f"prec={dm['precision']}  rec={dm['recall']}")
print(f"    cost-benefit   : thr={cb['optimal_threshold']}  "
      f"prec={cb['model_precision']}  rec={cb['model_recall']}")
check("两接口阈值一致",
      abs(ri["decision_threshold"] - cb["optimal_threshold"]) < 1e-9,
      f"{ri['decision_threshold']} vs {cb['optimal_threshold']}")
check("两接口 precision 一致",
      abs(dm["precision"] - cb["model_precision"]) < 1e-6)
check("两接口 recall 一致",
      abs(dm["recall"] - cb["model_recall"]) < 1e-6)

print()
print("=" * 78)
print("F) 数据来源如实标注")
print("=" * 78)
check("retention.basis 标为 actual_but_seeded（非 actual）",
      rs.get("basis") == "actual_but_seeded", f"= {rs.get('basis')}")
check("note 说明数据为播种演示数据",
      "播种" in (rs.get("note") or "") and "seed_work_orders" in (rs.get("note") or ""))
check("note 说明两个 ROI 不可比",
      "不可直接比较" in (rs.get("note") or ""))
print(f"    note = {rs.get('note')[:150]}...")

print()
print("=" * 78)
print(f"结果: {'全部通过' if not fails else '未通过 → ' + ', '.join(fails)}")
print("=" * 78)
