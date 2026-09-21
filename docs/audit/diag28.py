"""第二十八轮：验证 _REASON_MAP 关键词修复效果。"""
import sys
sys.path.insert(0, "/tmp/audit")
from app.services import risk_scoring as rs
from app.celery_tasks.predict import SHAP_FEATURE_LABELS as L

print("=" * 88)
print("一、_REASON_MAP 结构校验（向量化路径的硬约束）")
print("=" * 88)
print(f"  条目数 = {len(rs._REASON_MAP)}  (必须为 9)")
print(f"  首位关键词 = {[k[0][0] for k in rs._REASON_MAP]}")
print(f"  条目数 == 9: {'✅' if len(rs._REASON_MAP) == 9 else '❌'}")

print()
print("=" * 88)
print("二、SHAP 标签 → reason 的匹配情况")
print("=" * 88)
ok = 0
for feat, label in L.items():
    m = [t for kw, t in rs._REASON_MAP if any(k in label for k in kw)]
    if m:
        ok += 1
    state = m[0] if m else "*** 无匹配 ***"
    print(f"  {feat:22s} 「{label:14s}」 -> {state}")
print(f"\n  匹配率: {ok}/{len(L)}")

print()
print("=" * 88)
print("三、规则标签（_risk_factors 路径）是否仍正确匹配")
print("=" * 88)
rule_factors = ["有投诉记录", "非活跃用户", "持有 3 个产品（产品超载）",
                "高龄客户（55 岁）", "账户余额为零", "德国地区客户",
                "满意度偏低（1/5）", "信用评分偏低（550 分）",
                "在网时长较短（1 年）"]
for f in rule_factors:
    m = [t for kw, t in rs._REASON_MAP if any(k in f for k in kw)]
    print(f"  {f:32s} -> {m[0] if m else '*** 无匹配 ***'}")

print()
print("=" * 88)
print("四、关键回归：多因素时「首项 == reason 来源」的比例")
print("=" * 88)
# 构造多种因素组合
tests = [
    ["持有产品数（75.8%）", "活跃状态（11.3%）", "性别（5.0%）"],
    ["年龄（60.0%）", "持有产品数（20.0%）"],
    ["账户余额（50.0%）", "满意度评分（30.0%）"],
    ["地区（40.0%）", "信用评分（35.0%）"],
    ["持有信用卡（45.0%）", "在网时长（25.0%）"],
    ["预估薪资（55.0%）", "积分（20.0%）"],
    ["满意度评分（40.0%）", "持有产品数（30.0%）"],
    ["有投诉记录", "高龄客户（60 岁）"],
]
good = 0
for factors in tests:
    rec = rs.recommend_action("LOW", "HIGH", factors)
    first = factors[0]
    first_is_src = any(any(k in first for k in kw) and t == rec["reason"]
                       for kw, t in rs._REASON_MAP)
    if first_is_src:
        good += 1
    mark = "✅" if first_is_src else "⚠"
    print(f"  {mark} 首项={first:24s} -> reason={rec['reason']}")
print(f"\n  首项即 reason 来源: {good}/{len(tests)}")
print()
print("  ⚠ 未达 100% 是**预期**的：reason 是按 _REASON_MAP 的固定优先级")
print("     （投诉 > 产品 > 非活跃 > 余额 > …）选的，而 factors 按 SHAP 贡献")
print("     降序。两者排序依据不同，首项未必是优先级最高者。")
print("     修复目标是「reason 必须来自 factors 中的某一项」，而非「必须是首项」。")

print()
print("=" * 88)
print("五、关键断言：reason 是否总能从 factors 中找到来源")
print("=" * 88)
miss = 0
for factors in tests:
    rec = rs.recommend_action("LOW", "HIGH", factors)
    reason = rec["reason"]
    # ① 来自 _REASON_MAP 的规范文案：须能在 factors 里找到触发关键词
    from_map = any(t == reason for _kw, t in rs._REASON_MAP)
    if from_map:
        ok = any(any(k in f for k in kw) and t == reason
                 for f in factors for kw, t in rs._REASON_MAP)
        how = "REASON_MAP"
    else:
        # ② 兜底文案：形如「<首项去占比>是主要风险来源，建议优先核实」
        head = reason.split("是主要风险来源")[0] if "是主要风险来源" in reason else None
        ok = bool(head) and any(f.split("（")[0].strip() == head for f in factors)
        how = "兜底(factors首项)"
    if not ok:
        miss += 1
        print(f"  ❌ reason='{reason}' 无法从 {factors} 推出")
    else:
        print(f"  ✅ [{how}] {reason}")
print(f"\n  无来源次数: {miss}/{len(tests)}")
print(f"  → {'✅ 全部可溯源' if miss == 0 else '❌ 存在不可溯源的 reason'}")
