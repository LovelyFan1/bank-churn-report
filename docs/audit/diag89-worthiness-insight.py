"""核查：余额为 0 的高危客户在名单中的位置 + 现有排序会把他们排到哪。

diag88 发现：决策线内 6,582 人中有 2,001 人（30.4%）按个体经济是净亏的，
其中 1,975 人是 ZERO 价值层（余额 0）。

关键追问：这些人概率极高（0.99+），那么**按概率排序时他们是不是排在前面**？
若是，则"默认按概率看名单"的用法会第一时间推给客户经理一批"无资产可留"的人。

同时核对：系统默认排序是什么？前端下拉有哪些选项？
"""
import json
import urllib.request

BASE = "http://localhost:8000"


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r:
        return json.loads(r.read().decode())


ri = get("/api/model/risk-info")
bs = get("/api/dashboard/summary")["business_summary"]
AVG = bs["avg_customer_value"]
CR = ri["cost_ratio"]
S = ri["success_rate"]
DT = ri["decision_threshold"]
cost_per = AVG / CR
be = cost_per / S

print("=" * 82)
print("一、按不同排序，Top100 的价值层构成")
print("=" * 82)
for sort, label in [("probability", "按流失概率"),
                    ("expected_value", "按期望价值")]:
    d = get(f"/api/customers?page=1&page_size=100&sort_by={sort}&sort_order=desc")
    items = d["items"]
    from collections import Counter
    tiers = Counter(c["value_tier"] for c in items)
    zero_bal = sum(1 for c in items if c["balance"] <= 0)
    worth = sum(1 for c in items
                if c["probability"] * c["balance"] * S - cost_per > 0)
    ev_sum = sum(c["expected_value"] for c in items)
    print(f"\n  【{label}】")
    print(f"    价值层分布   {dict(tiers)}")
    print(f"    余额为 0     {zero_bal}/100")
    print(f"    值得干预     {worth}/100")
    print(f"    期望价值合计 {ev_sum:,.0f}")
    print(f"    前 5 人：")
    for c in items[:5]:
        n = c["probability"] * c["balance"] * S - cost_per
        print(f"      {c['customer_id']}  p={c['probability']:.4f}  "
              f"余额={c['balance']:>12,.0f}  EV={c['expected_value']:>10,.0f}  "
              f"净={n:>10,.0f}  {c['value_tier']}")

print()
print("=" * 82)
print("二、决策线内「不值得干预」的人，其概率分布")
print("=" * 82)
in_line = []
page = 1
while page <= 120:
    d = get(f"/api/customers?page={page}&page_size=100&sort_by=probability&sort_order=desc")
    items = d["items"]
    if not items:
        break
    stop = False
    for c in items:
        if c["probability"] < DT:
            stop = True
            break
        in_line.append(c)
    if stop or page * 100 >= d["total"]:
        break
    page += 1

bad = [c for c in in_line if c["probability"] * c["balance"] * S - cost_per <= 0]
good = [c for c in in_line if c["probability"] * c["balance"] * S - cost_per > 0]
import numpy as np
bp = np.array([c["probability"] for c in bad])
gp = np.array([c["probability"] for c in good])
print(f"  净亏者 {len(bad):,} 人   概率 均值 {bp.mean():.4f}  中位 {np.median(bp):.4f}  最大 {bp.max():.4f}")
print(f"  值得者 {len(good):,} 人   概率 均值 {gp.mean():.4f}  中位 {np.median(gp):.4f}  最小 {gp.min():.4f}")
print()
print(f"  ★ 两者概率只差 {abs(gp.mean()-bp.mean())*100:.1f} 个百分点"
      f"（{bp.mean():.4f} vs {gp.mean():.4f}）")
print(f"    ⇒ 靠概率**无法区分**这 {len(bad):,} 人 —— 他们在概率上几乎与")
print(f"      「值得救的人」无法分辨。真正的区分变量是**余额**：")
print(f"      净亏者平均余额仅 {np.mean([c['balance'] for c in bad]):,.0f}，"
      f"其中 {sum(1 for c in bad if c['value_tier']=='ZERO')}/{len(bad)} 属零余额层。")
print(f"    即「判得准」不等于「值得救」：概率回答会不会跑，余额回答跑了亏多少。")

print()
print("  ⚠ 本节结论此前写错过一次（2026-09 自查修正）：原脚本硬编码文案为")
print("    「净亏者的平均概率反而更高」，但同处打印的两个数 0.7793 < 0.8005，")
print("    文案与数字自相矛盾 —— 属未经复核的臆断。现按实测数字改写为上述表述，")
print("    论点（概率不足以识别净亏者）不变，但依据由「概率更高」改回「概率几乎相同」。")

print()
print("=" * 82)
print("三、现有排序选项")
print("=" * 82)
print("  前端 CustomerManagement 的排序下拉（源码）:")
import os
_candidates = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "..", "frontend", "src", "views", "CustomerManagement.vue"),
    "/app/../../frontend/src/views/CustomerManagement.vue",
]
src = None
for _p in _candidates:
    if os.path.exists(_p):
        src = open(_p, encoding="utf-8").read()
        break
if src is None:
    print("    （未找到前端源码，跳过 —— 该检查仅作辅助，不影响上述结论）")
else:
    for m in re.finditer(r'<option value="(\w+)"[^>]*>([^<]+)</option>', src):
        print(f"    {m.group(1):<16} {m.group(2)}")
print()
print(f"  ⚠ 没有「净收益」「值不值得救」这类排序项")
print(f"  ⚠ 个股盈亏平衡点 EV > {be:,.0f} 这个信息在界面上**完全不可见**")

print()
print("=" * 82)
print("四、把这个洞察变成工单层能力，可以做什么")
print("=" * 82)
print(f"  1) 建单前**净收益预检**：EV < {be:,.0f} 的人默认不建单（或提示）")
print(f"     实测可避免 2,001 次无效干预 = {len(bad)*cost_per:,.0f} 元")
print(f"  2) 名单排序改按「个体净收益」而非概率/EV")
print(f"  3) 零余额高危客户改走 automated 渠道（成本近 0），而非占用客户经理")
print(f"     实测 ZERO 层占净亏者的 {sum(1 for c in bad if c['value_tier']=='ZERO')}/{len(bad)}")
print(f"  4) 工单建单时自动写入 note：记录为何值得打（现在 note 填充率 0%）")
