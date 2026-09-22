"""核查：个体经济性与总体经济性是否同源。

背景：我要在建单弹窗里生成一句「这个人值不值得救」，它要用**个体余额**算：
    net_i = p_i × balance_i × s − cost_per
而看板的年度损失用的是**平均客户价值假设**：
    annual_loss = churn_count × AVG_CUSTOMER_VALUE
若这两处的"客户值多少钱"不是一个数，那我在弹窗里说的
「期望可挽回 ¥38,000」就与看板上的口径打架 —— 正是本项目一直在消除的
「同一件事两个数」的第 7 例。

本脚本量化这个差异。
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
cost_per = AVG / CR
be = cost_per / S

ov = get("/api/dashboard/summary")["overview"]
churn_n = ov["churned_customers"]

print("=" * 80)
print("一、两个「客户值多少钱」")
print("=" * 80)
print(f"  AVG_CUSTOMER_VALUE（假设值，用于看板年度损失）  ¥{AVG:,.0f}")

# 真实余额分布：全量客户 + 仅流失客户
tot = 0
tot_bal = 0
churn_bal = 0
churn_cnt = 0
alive_bal = 0
alive_cnt = 0
zero_bal_churn = 0
zero_bal_all = 0
page = 1
while True:
    d = get(f"/api/customers?page={page}&page_size=100")
    items = d["items"]
    if not items:
        break
    for c in items:
        b = c["balance"] or 0
        tot += 1
        tot_bal += b
        if b <= 0:
            zero_bal_all += 1
        if c["exited"] == 1:
            churn_cnt += 1
            churn_bal += b
            if b <= 0:
                zero_bal_churn += 1
        else:
            alive_cnt += 1
            alive_bal += b
    if page * 100 >= d["total"]:
        break
    page += 1

print(f"  全量客户实际平均余额                          ¥{tot_bal/tot:,.0f}"
      f"   (n={tot:,})")
print(f"  流失客户实际平均余额                          ¥{churn_bal/churn_cnt:,.0f}"
      f"   (n={churn_cnt:,})")
print(f"  未流失客户实际平均余额                        ¥{alive_bal/alive_cnt:,.0f}"
      f"   (n={alive_cnt:,})")
print()
print(f"  ⚠ 看板年度损失 = {churn_n:,} × ¥{AVG:,.0f} = ¥{churn_n*AVG:,.0f}")
print(f"    实际流失客户余额合计              = ¥{churn_bal:,.0f}")
print(f"    比值 = {churn_n*AVG/churn_bal:.2f}x  "
      f"（看板 {'高估' if churn_n*AVG > churn_bal else '低估'}）")
print()
print(f"  流失客户中零余额占比  {zero_bal_churn/churn_cnt*100:.1f}%"
      f"  ({zero_bal_churn:,}/{churn_cnt:,})")
print(f"  全量客户中零余额占比  {zero_bal_all/tot*100:.1f}%")

print()
print("=" * 80)
print("二、个体经济性参数")
print("=" * 80)
print(f"  单次干预成本 cost_per = AVG/CR = {AVG:,.0f}/{CR} = ¥{cost_per:,.0f}")
print(f"  盈亏平衡期望价值 be  = cost_per/S = {cost_per:,.0f}/{S} = ¥{be:,.2f}")
print()
print("  个体净收益 net_i = p_i × balance_i × S − cost_per")
print("  总体净收益 net   = TP×(S×CR−1) − FP        （单位=一次干预成本）")
print()
print("  两者关系（设被触达集合 N=TP+FP，流失者余额均值≈B）：")
print("    Σ net_i = S×Σ(p_i×b_i) − N×c")
print("            ≈ S×TP×B − (TP+FP)×c")
print("            = TP×(S×B − c) − FP×c")
print("    除以 c  ⇒  TP×(S×B/c − 1) − FP = TP×(S×CR − 1) − FP   ✔ 同构")
print()
print(f"  ★ 结论：公式同构，**前提是 B = AVG_CUSTOMER_VALUE**。")
print(f"    实际 B（流失客户平均余额）= ¥{churn_bal/churn_cnt:,.0f}")
print(f"    假设 AVG_CUSTOMER_VALUE     = ¥{AVG:,.0f}")
d = abs(churn_bal/churn_cnt - AVG) / AVG * 100
print(f"    偏差 {d:.1f}%")
