"""第二十九轮：核实规则标签路径下 reason 的「不可溯源」是否真实。

⚠ 疑点：向量化路径的 factors 是规则标签（如「持有 3 个产品（产品超载）」），
   我的校验脚本按「关键词 in factor」查找，但 reason='产品数量过多…' 的
   关键词是 ["产品超载","持有产品数","产品数量"]，而 factor 里写的是
   「持有 3 个产品（产品超载）」—— 含「产品超载」，应该能匹配。
   所以 269 例更可能是**别的原因**。必须查清具体是哪一条。
"""
import sys
sys.path.insert(0, "/tmp/audit")
from app.services import risk_scoring as rs
from app.database import SessionLocal
import collections

db = SessionLocal()
sc = rs.get_scored_customers(db)

bad = []
for x in sc[:5000]:
    r = x["reason"]; fs = x.get("risk_factors") or []
    ok = False
    for kw, t in rs._REASON_MAP:
        if t == r and any(any(k in f for k in kw) for f in fs):
            ok = True; break
    if not ok and "是主要风险来源" in r:
        head = r.split("是主要风险来源")[0]
        if any(f.split("（")[0].strip() == head for f in fs):
            ok = True
    if not ok:
        bad.append((x["customer_id"], r, fs))

print("=" * 90)
print(f"不可溯源样本数: {len(bad)}")
print("=" * 90)
c = collections.Counter((r, tuple(fs[:3])) for _cid, r, fs in bad)
for (r, fs), n in c.most_common(10):
    print(f"\n  {n:4d} 例  reason = 「{r}」")
    print(f"        factors = {list(fs)}")

print()
print("=" * 90)
print("归因分析：这些 reason 的关键词是否出现在 factors 中")
print("=" * 90)
for (r, fs), n in c.most_common(5):
    kws = [kw for kw, t in rs._REASON_MAP if t == r]
    print(f"\n  reason = 「{r}」")
    print(f"    其关键词组 = {kws}")
    for f in fs:
        hit = [k for kw in kws for k in kw if k in f]
        print(f"      factor「{f}」 → 命中关键词: {hit if hit else '无'}")

print()
print("=" * 90)
print("结论判定")
print("=" * 90)
# 真正的问题是：空 factors 的客户
empty = [b for b in bad if not b[2]]
print(f"  factors 为空的样本: {len(empty)}")
if empty:
    print(f"    → 这些客户没有任何风险因素，reason 给默认文案是正确的")
    for cid, r, fs in empty[:5]:
        print(f"       {cid}: reason=「{r}」factors={fs}")
nonempty = [b for b in bad if b[2]]
print(f"  factors 非空但仍不可溯源: {len(nonempty)}")
for cid, r, fs in nonempty[:5]:
    print(f"       {cid}: reason=「{r}」factors={fs}")

db.close()
