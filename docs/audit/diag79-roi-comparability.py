"""业务逻辑审计（三）—— 两个 ROI 是否真的可比。

代码注释声称：「复用与推算值同一套成本假设，差异只来自『实际挽留了多少人』
这一点，两组数字才可比」。

本脚本验证这句话是否成立 —— 即两个 ROI 的分母与分子是否同一口径。
"""
import json
import urllib.request

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=240) as r:
        return json.loads(r.read().decode("utf-8"))


bs = get("/api/dashboard/summary")["business_summary"]
rs = get("/api/cost-benefit/retention-summary")
AVG = bs["avg_customer_value"]
CR = 5.0
N = bs["total_customers"]

print("=" * 80)
print("推算 ROI vs 实测 ROI —— 逐项拆解")
print("=" * 80)

print("\n【推算值】(basis=estimate)")
print(f"    分子 reduced_loss       = {bs['retained_customers']:,} × {AVG:,.0f}"
      f" = {bs['reduced_loss']:,.0f}")
print(f"    分母 intervention_cost  = {bs['annual_flagged']:,} × {bs['cost_per_intervention']:,.0f}"
      f" = {bs['intervention_cost']:,.0f}")
print(f"    ROI = {bs['reduced_loss']:,.0f} / {bs['intervention_cost']:,.0f} = {bs['roi']}")
print(f"    等价式 = CR × precision = {CR} × {bs['model_precision']} = {CR*bs['model_precision']:.2f}")
print(f"    ⚠ 这里的 'retained_customers' 其实是 **TP**（判对的人数），")
print(f"      不是'真的挽留成功的人'。分子把 TP 当成了'挽回成功'。")

print("\n【实测值】(basis=actual)")
print(f"    完成工单数 = {rs['total_completed']}")
print(f"    分子 benefit = {rs['retained']} × {AVG:,.0f} = {rs['benefit']:,.0f}")
print(f"    分母 cost    = {rs['total_completed']} × {AVG/CR:,.0f} = {rs['cost']:,.0f}")
print(f"    ROI = {rs['benefit']:,.0f} / {rs['cost']:,.0f} = {rs['roi']}")
print(f"    等价式 = CR × retained/total = {CR} × {rs['retained']}/{rs['total_completed']}"
      f" = {CR*rs['retained']/rs['total_completed']:.2f}")

print("\n" + "=" * 80)
print("口径对比")
print("=" * 80)
rows = [
    ("分母：算作'被触达'的人", f"{bs['annual_flagged']:,}", f"{rs['total_completed']}"),
    ("分子：算作'挽回'的人", f"{bs['retained_customers']:,}", f"{rs['retained']}"),
    ("实际比率", f"{bs['model_precision']:.4f} (precision)", f"{rs['success_rate']:.4f} (retained/total)"),
    ("ROI", f"{bs['roi']}", f"{rs['roi']}"),
]
print(f"    {'项目':<28}{'推算值':>26}{'实测值':>22}")
for a, b, c in rows:
    print(f"    {a:<28}{b:>26}{c:>22}")

print()
print("★ 关键问题：两者分母相差 %d 倍" % (bs["annual_flagged"] / rs["total_completed"]))
print(f"    推算：把「决策线覆盖的全部 {bs['annual_flagged']:,} 人」算作触达成本")
print(f"    实测：只把「已经建了单的 {rs['total_completed']} 条」算作触达成本")
print()
print("    而 precision 与 retained/total 是**两个不同的比率**：")
print("      precision      = 判为流失的人里，真的是流失者的比例（模型准不准）")
print("      retained/total = 建单完成的人里，挽留成功的比例（干预灵不灵）")
print()
print("    所以 '实测 ROI 更高(3.04 > 2.15)' 是**假象**：")
print("      它只是因为分母被缩小到 56 条，而不是因为实际做得更好。")
print(f"    若按推算同样的分母口径，实测 ROI 应为：")
print(f"      CR × precision × 挽留成功率 = {CR} × {bs['model_precision']}"
      f" × {rs['success_rate']:.3f} = {CR*bs['model_precision']*rs['success_rate']:.2f}")
print(f"    即真实 ROI ≈ {CR*bs['model_precision']*rs['success_rate']:.2f}，"
      f"**低于**页面显示的 {rs['roi']}")

print()
print("=" * 80)
print("绝对规模对比（ROI 高 ≠ 影响大）")
print("=" * 80)
print(f"    推算年减少损失 = {bs['reduced_loss']:,.0f} 元")
print(f"    实测实际挽回   = {rs['benefit']:,.0f} 元")
print(f"    覆盖率 = {rs['benefit']/bs['reduced_loss']*100:.3f}%")
print()
print(f"    → 实测挽回了 {rs['retained']} 人，而模型判定有 {bs['retained_customers']:,} 人值得挽回")
print(f"      执行覆盖 = {rs['retained']/bs['retained_customers']*100:.2f}%")
print("      即：ROI 看着不错，但盘子只动了极小一角")
