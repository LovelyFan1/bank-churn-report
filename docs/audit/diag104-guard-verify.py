"""验证 Guard 拒答规则：该拦的拦、该放的放。

拒答层如果拦错，用户会看到「这个我答不了」而其实能答 —— 体验劣化；
如果漏拦，LLM 会编造数字 —— 后果严重。故两侧都要测。

本脚本直接调用 guard_rules.check()，不发网络请求、不调 LLM。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "backend"))

from app.agent import guard_rules  # noqa: E402

SHOULD_BLOCK = [
    ("真实挽留成功率是多少", "retention_success_rate"),
    ("这批客户实际留住了多少", "retention_success_rate"),
    ("干预效果如何", "retention_success_rate"),
    ("明年的流失趋势怎样", "temporal_trend"),
    ("预测一下未来的客户流失", "temporal_trend"),
    ("下个季度流失率会增长吗", "temporal_trend"),
    ("哪个策略效果最好", "strategy_comparison"),
    ("客户经理和外呼哪个渠道更有效", "strategy_comparison"),
    ("哪个负责人的挽留效果最好", "strategy_comparison"),
]

SHOULD_PASS = [
    # 能答的常规问题
    ("C034525 这个人要不要打电话", None),
    ("现在决策阈值是多少", None),
    ("极高风险的客户有哪些", None),
    ("余额为 0 的高危客户有多少", None),
    ("工单处理情况怎么样", None),
    ("谁的工单最多", None),
    # 负责人维度：落到"具体可查的量"时应放行（不该被业绩对比规则误拦）
    ("张思远手上多少单", None),
    ("各负责人工单数分布", None),
    # 回归：把「风险排名」误拦是过度拦截（正常请求）
    ("给我看风险排名前十", None),
    ("按期望价值排个序", None),
    # 豁免一：含具体客户编号 → 一定不拦
    ("C034525 明年会流失吗", "含客户编号应豁免"),
    # 豁免二：句里同时含可答主题 → 整句交给 LLM
    ("明年的流失趋势如何，另外现在高危有多少人", "含可答主题应豁免"),
    ("哪个策略更好，顺便看下当前阈值", "含可答主题应豁免"),
]

print("=" * 88)
print("一、应当拦截（答不了的三类）")
print("=" * 88)
fails = []
for q, expect_key in SHOULD_BLOCK:
    hit = guard_rules.check(q)
    if hit is None:
        fails.append(f"漏拦：{q}")
        print(f"  [FAIL 漏拦] {q}")
    elif hit.key != expect_key:
        fails.append(f"拦错主题：{q} → {hit.key}，期望 {expect_key}")
        print(f"  [FAIL 主题错] {q} → {hit.key}，期望 {expect_key}")
    else:
        print(f"  [OK] {q}  →  {hit.key}")

print()
print("=" * 88)
print("二、应当放行")
print("=" * 88)
for q, note in SHOULD_PASS:
    hit = guard_rules.check(q)
    if hit is None:
        print(f"  [OK] {q}" + (f"   （{note}）" if note else ""))
    else:
        fails.append(f"误拦：{q} → {hit.key}")
        print(f"  [FAIL 误拦] {q} → {hit.key}" + (f"   （{note}）" if note else ""))

print()
print("=" * 88)
print("三、拒答文案质量（必须给出实测理由与出口，不能只说「不知道」）")
print("=" * 88)
for q, _ in SHOULD_BLOCK[:1] + SHOULD_BLOCK[3:4] + SHOULD_BLOCK[6:7]:
    hit = guard_rules.check(q)
    msg = hit.to_message()
    has_why = len(hit.reason) > 40
    has_instead = "可以问" in msg
    has_fact = any(k in hit.reason for k in
                   ["硬编码", "时间语义", "1 行", "假设值", "零个"])
    mark = "OK" if (has_why and has_instead and has_fact) else "FAIL"
    if mark == "FAIL":
        fails.append(f"文案不合格：{hit.key}")
    print(f"  [{mark}] {hit.key}  理由={len(hit.reason)}字 含实测事实={has_fact} 有出口={has_instead}")

print()
print("=" * 88)
print("四、边界输入")
print("=" * 88)
for q in ["", "   ", None]:
    try:
        hit = guard_rules.check(q)
        print(f"  [OK] {q!r} → {hit}")
    except Exception as e:
        fails.append(f"边界输入崩溃：{q!r} {type(e).__name__}")
        print(f"  [FAIL] {q!r} → {type(e).__name__}: {e}")

print()
print("=" * 88)
print(f"答不了的主题清单（对外暴露 {len(guard_rules.known_topics())} 条）")
print("=" * 88)
for t in guard_rules.known_topics():
    print(f"  · {t['key']}")

print()
if fails:
    print(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        print(f"  · {f}")
else:
    print("结论：PASS —— 该拦的拦、该放的放、文案含实测依据与出口")
