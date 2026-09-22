"""验证数字保全校验器：该抓的抓、该放的放。

这是本项目「不幻觉」的机械保障，两侧都要严格测：
  · 误杀 → 正确回答被丢弃，用户频繁看到降级提示（体验劣化）
  · 漏放 → 编造的数字流到用户面前（后果严重）

用例全部取自真实数据形状（96,418 行、LightGBM、阈值 0.60）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "backend"))

from app.agent import verify as V  # noqa: E402

# 模拟真实的工具返回（形状与 diag105 实测一致）
TOOL_RESULTS = [
    {"total": 4821, "returned": 3, "matched_total": 4821, "is_complete": False},
    {"items": [
        {"customer_id": "C071081", "balance": 225563.40, "probability": 0.9757,
         "expected_value": 220080.25, "value_tier": "HIGH"},
        {"customer_id": "C034525", "balance": 219490.05, "probability": 0.9620,
         "expected_value": 211139.71, "value_tier": "HIGH"},
        {"customer_id": "C067546", "balance": 0, "probability": 0.9981,
         "expected_value": 0, "value_tier": "ZERO"},
        # 第三个客户 —— 补上是为了让"表格含排名列"用例能完整通过；
        # 缺它时测试会把真实数字误报为孤儿（测试夹具问题，非代码问题）
        {"customer_id": "C062858", "balance": 225534.51, "probability": 0.8883,
         "expected_value": 200331.86, "value_tier": "HIGH"},
    ]},
    {"decision_threshold": 0.6, "cost_ratio": 5.0, "success_rate": 0.3,
     "thresholds_grading": {"critical": 0.6825, "high": 0.2468, "medium": 0.0683},
     "cost_per_intervention": 10000.0},
    {"worthiness": {"verdict": "worth", "net": 53341.61,
                    "breakeven": 33333.33, "ratio": 5.8864}},
]

SHOULD_PASS = [
    ("直接引用原值",
     "极高风险客户共 4,821 人，最高期望价值 220,080.25 元。"),
    ("千分位换算",
     "共 4,821 人，期望价值 220,080 元。"),
    ("比例转百分比（上轮实测的误杀点）",
     "挽回成功率 30%，成本收益比 5.0，决策阈值 0.6。"),
    ("百分比转比例",
     "成功率约 30% 时，盈亏平衡点 33,333.33 元。"),
    ("四舍五入到整数",
     "该客户期望价值约 220080 元，个体净收益 53342 元。"),
    ("万元换算",
     "期望价值约 22.0 万元。"),
    ("Markdown 有序列表（上轮实测的误杀点）",
     "结论如下：\n1. 极高风险共 4821 人\n2. 决策阈值 0.6\n3. 成本比 5.0"),
    ("中文序号",
     "两点说明：（1）阈值 0.6；（2）成本比 5.0。"),
    ("第N点格式",
     "第一点，阈值 0.6。第二点，成功率 30%。"),
    ("无数字的纯定性回答",
     "该客户风险极高，建议优先联系客户经理。"),
    # ── 实测误杀回归（diag111）：Markdown 表格的排名列 ──
    # 用户实测「帮我调出挽回价值最高的3个客户」时，模型用表格列出，
    # Rank 列写 | 1 | | 2 | | 3 |，其中 `2` 不在工具返回值里 →
    # 整段正确回答被判"含无法溯源数字"并丢弃。
    # 更糟的是不稳定：1 与 3 恰好因 round(概率) 和 参数 limit 混进白名单。
    ("Markdown 表格含排名列（实测误杀）",
     "# 挽回价值最高的 3 个客户\n\n"
     "| 排名 | 客户ID | 余额 | 流失概率 | 预期挽回价值 |\n"
     "|------|--------|------|----------|--------------|\n"
     "| 1 | C071081 | 225,563.4 | 0.9757 | 220,080.25 |\n"
     "| 2 | C034525 | 219,490.05 | 0.962 | 211,139.71 |\n"
     "| 3 | C062858 | 225,534.51 | 0.8883 | 200,331.86 |\n"),
    ("Markdown 表格含序号列（英文表头）",
     "| # | 客户ID | 概率 |\n|---|---|---|\n"
     "| 1 | C071081 | 0.9757 |\n| 2 | C034525 | 0.962 |\n"),
    ("无表头的行首序号列",
     "| 1 | C071081 | 0.9757 |\n| 2 | C034525 | 0.962 |\n"),
]

SHOULD_FAIL = [
    ("凭空编造的总数",
     "极高风险客户共 12,345 人，其中值得干预的约 9,876 人。"),
    ("错报阈值",
     "当前决策阈值是 0.55，成功率 45%。"),
    ("编造的金额",
     "预计可挽回损失 8,888,888 元。"),
    ("错报的客户余额",
     "该客户余额 999,999 元，流失概率 0.99。"),
    ("编造的百分比",
     "挽留成功率实测约 61.5%。"),
    # ── 反向：不得因剥序号而放过表格中间列的数据错误 ──
    # 表格首列是排名（该剥），但中间列若是数据（余额），编造必须被抓。
    ("表格中间列编造余额（不得因剥序号而放过）",
     "| 排名 | 客户 | 余额 |\n|---|---|---|\n"
     "| 1 | C071081 | 999,999 |\n| 2 | C034525 | 219,490.05 |\n"),
]

print("=" * 90)
print("一、应当通过（正确回答，不得误杀）")
print("=" * 90)
fails = []
for label, draft in SHOULD_PASS:
    ok, orphans = V.verify(draft, TOOL_RESULTS)
    tag = "OK  " if ok else "FAIL"
    if not ok:
        fails.append(f"误杀「{label}」→ 孤儿 {orphans}")
    print(f"  [{tag}] {label}")
    if not ok:
        print(f"         孤儿数字: {orphans}")

print()
print("=" * 90)
print("二、应当拦截（编造数字，必须抓住）")
print("=" * 90)
for label, draft in SHOULD_FAIL:
    ok, orphans = V.verify(draft, TOOL_RESULTS)
    tag = "OK  " if not ok else "FAIL"
    if ok:
        fails.append(f"漏放「{label}」")
    print(f"  [{tag}] {label}")
    print(f"         识别为孤儿: {orphans}")

print()
print("=" * 90)
print("三、边界情况")
print("=" * 90)
cases = [
    ("空回答", "", TOOL_RESULTS, True),
    ("空白回答", "   ", TOOL_RESULTS, True),
    ("无工具返回（纯知识问答）→ 跳过校验", "大约 30% 的客户会流失。", [], True),
    ("工具返回但回答无数字", "该客户风险很高。", TOOL_RESULTS, True),
]
for label, draft, results, expect in cases:
    ok, orphans = V.verify(draft, results)
    tag = "OK  " if ok == expect else "FAIL"
    if ok != expect:
        fails.append(f"边界「{label}」期望 {expect} 实得 {ok}")
    print(f"  [{tag}] {label} → passed={ok}")

print()
print("=" * 90)
print("四、换算白名单实际扩张了多少值（说明为何不会误杀）")
print("=" * 90)
src = set()
V._collect_numbers(TOOL_RESULTS, src)
exp = V._expand_allowed(src)
print(f"  工具返回值中的原始数字数: {len(src)}")
print(f"  扩张后的白名单大小:       {len(exp)}")
print(f"  平均每个值可接受 {len(exp)/max(len(src),1):.1f} 种表示形式")

print()
print("=" * 90)
if fails:
    print(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        print(f"  · {f}")
else:
    print("结论：PASS —— 正确回答全部放行（含比例换算与序号），编造数字全部拦截")
