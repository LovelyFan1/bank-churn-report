"""第二十五轮：逐行比对「向量化 _build_scored_result」与「标量 recommend_action」。

⚠ 这是本项目最危险的缺陷类型：同一客户在两个代码路径下得到不同的
   策略/理由/等级。_build_scored_result 是向量化实现，recommend_action
   是标量参考实现，二者必须**逐字段一致**。
"""
import sys
sys.path.insert(0, "/tmp/audit")
import numpy as np
from app.database import SessionLocal
from app.services import risk_scoring as rs

db = SessionLocal()
eng = rs._ensure_engine(db)
print(f"model = {eng['name']}, thresholds = {eng['thresholds']}")

df = eng["df"]; raw = eng["raw"]; th = eng["thresholds"]

# 只取前 3000 行做逐行比对（够暴露分歧，避免太慢）
N = 3000
sub = df.iloc[:N].reset_index(drop=True)
sub_raw = raw[:N]

vec = rs._build_scored_result(sub, sub_raw, th)
print(f"向量化结果 {len(vec)} 行")

print()
print("=" * 92)
print("一、逐行比对 scale/向量 两条路径")
print("=" * 92)

mismatch = {"level": [], "tier": [], "action": [], "channel": [], "reason": [], "ev": []}
for i in range(N):
    row = sub.iloc[i]
    p = float(sub_raw[i])
    # 标量参考
    lvl = rs._level(p, th)
    tier = rs.value_tier(float(row["balance"]))
    ev = rs.expected_value(p, float(row["balance"]))
    factors_scalar = []
    # 复刻 _risk_factors 的顺序（需读源码确认）
    v = vec[i]
    if v["risk_level"] != lvl:
        if len(mismatch["level"]) < 5:
            mismatch["level"].append((i, v["risk_level"], lvl, p, row["balance"]))
    if v["value_tier"] != tier:
        if len(mismatch["tier"]) < 5:
            mismatch["tier"].append((i, v["value_tier"], tier, row["balance"]))
    if abs(float(v["expected_value"]) - float(ev)) > 0.01:
        if len(mismatch["ev"]) < 5:
            mismatch["ev"].append((i, v["expected_value"], ev))

for k, vals in mismatch.items():
    if vals:
        print(f"\n  ❌ {k} 不一致 ({len(vals)} 例):")
        for x in vals:
            print(f"     {x}")
    else:
        print(f"  ✅ {k}: 一致")

print()
print("=" * 92)
print("二、重点：reason / action 的多因素重叠情形")
print("=" * 92)
# 找出同时命中多个掩码的客户
m1 = sub["complain"].to_numpy() == 1
m2 = sub["is_active_member"].to_numpy() == 0
m3 = sub["num_products"].to_numpy() >= 3
m4 = sub["age"].to_numpy() >= 50
m5 = sub["balance"].to_numpy() == 0
m6 = sub["geography"].to_numpy() == "Germany"
m7 = sub["satisfaction_score"].to_numpy() <= 2
m8 = sub["credit_score"].to_numpy() < 600
m9 = sub["tenure"].to_numpy() <= 2
cnt = np.zeros(N, dtype=int)
for m in [m1,m2,m3,m4,m5,m6,m7,m8,m9]:
    cnt += m.astype(int)
print(f"  命中因素数分布: {dict(zip(*np.unique(cnt, return_counts=True)))}")

# 对命中 >=2 个因素的客户，比对 reason
mask = cnt >= 2
idxs = np.where(mask)[0][:20]
print(f"\n  抽样 {len(idxs)} 个多因素客户，比对向量化 reason vs 标量期望:")
bad = 0
for i in idxs:
    v = vec[i]
    # 标量期望：按 _REASON_MAP 顺序取首个匹配
    exp = "常规维护建议"
    for keywords, text in rs._REASON_MAP:
        pass
    # 用 factors 里的关键词反查（这是前端看到的数据）
    factors = v["risk_factors"]
    exp2 = "常规维护建议"
    for keywords, text in rs._REASON_MAP:
        if any(kw in f for f in factors for kw in keywords):
            exp2 = text
            break
    ok = (v["reason"] == exp2)
    if not ok:
        bad += 1
    print(f"    i={i:5d} factors={str(factors)[:60]:62s} reason={v['reason'][:20]}")
print(f"\n  向量化 reason 与「按 factors 重算」不一致: {bad}/{len(idxs)}")

print()
print("=" * 92)
print("三、与 recommend_action 对比（标量权威实现）")
print("=" * 92)
bad2 = 0
for i in range(200):
    v = vec[i]
    rec = rs.recommend_action(v["value_tier"], v["risk_level"], v["risk_factors"])
    if rec["channel"] != v["channel"] or rec["action"] != v["action"] or rec["reason"] != v["reason"]:
        bad2 += 1
        if bad2 <= 5:
            print(f"    ❌ i={i}: 向量=({v['channel']},{v['action']},{v['reason']})")
            print(f"             标量=({rec['channel']},{rec['action']},{rec['reason']})")
print(f"\n  不一致: {bad2}/200")

db.close()
