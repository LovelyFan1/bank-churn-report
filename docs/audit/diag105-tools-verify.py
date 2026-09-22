"""验证工具层：6 个工具是否真能取到数据，且返回形状统一。

工具是 Agent 的手。手不好使，上面搭什么都是空中楼阁。
故在写图之前先把每个工具单独跑一遍。

⚠ 写工具只验证"返回待确认动作"，**不真的建单** —— 生产库 62 条工单是真实数据。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "backend"))

from app.agent import tools  # noqa: E402

fails = []

print("=" * 88)
print("一、注册表")
print("=" * 88)
print(f"  只读工具 {len(tools.READ_TOOL_NAMES)} 个: {tools.READ_TOOL_NAMES}")
print(f"  写工具   {len(tools.WRITE_TOOL_NAMES)} 个: {tools.WRITE_TOOL_NAMES}")

print()
print("=" * 88)
print("二、逐工具调用")
print("=" * 88)

CASES = [
    ("get_customer_risk", {"customer_id": "C034525"}),
    ("list_customers", {"risk_level": "CRITICAL", "sort_by": "expected_value", "limit": 3}),
    ("list_customers", {"value_tier": "LOW", "sort_by": "balance", "limit": 3}),
    ("get_model_thresholds", {}),
    ("get_business_summary", {}),
    ("get_workorder_stats", {}),
    ("propose_create_work_order", {"customer_id": "C034525", "assignee": "测试"}),
]

for name, args in CASES:
    r = tools.call_tool(name, args)
    ok = r.get("ok")
    tag = "OK " if ok else "FAIL"
    if not ok:
        fails.append(f"{name}{args} → {r.get('error')}")
    print(f"\n  [{tag}] {name}  {json.dumps(args, ensure_ascii=False)}")
    body = r.get("result") if ok else {"error": r.get("error")}
    txt = json.dumps(body, ensure_ascii=False)
    print(f"        {txt[:400]}{'…' if len(txt) > 400 else ''}")

print()
print("=" * 88)
print("三、关键断言")
print("=" * 88)

# 1) 单客户：必须带出真实的概率与余额
r = tools.call_tool("get_customer_risk", {"customer_id": "C034525"})
d = r.get("result", {})
asserts = [
    ("含 probability", isinstance(d.get("probability"), (int, float))),
    ("含 balance", isinstance(d.get("balance"), (int, float))),
    ("含 risk_level", bool(d.get("risk_level"))),
    ("含 worthiness（个体经济性）", isinstance(d.get("worthiness"), dict)),
    ("含 suggested_note", bool(d.get("suggested_note"))),
]
for label, ok in asserts:
    print(f"  [{'OK' if ok else 'FAIL'}] get_customer_risk {label}")
    if not ok:
        fails.append(f"get_customer_risk 缺 {label}")

# 2) list_customers：必须如实报告是否完整（防 Agent 谎称"这是全部"）
r = tools.call_tool("list_customers", {"risk_level": "CRITICAL", "limit": 3})
d = r.get("result", {})
lst = [
    ("returned == 3", d.get("returned") == 3),
    ("matched_total > returned（有更多）", (d.get("matched_total") or 0) > (d.get("returned") or 0)),
    ("is_complete == False", d.get("is_complete") is False),
    ("带 note 说明不完整", bool(d.get("note"))),
    ("sorted_by 回显正确", d.get("sorted_by") == "expected_value"),
]
for label, ok in lst:
    print(f"  [{'OK' if ok else 'FAIL'}] list_customers {label}")
    if not ok:
        fails.append(f"list_customers 断言失败: {label}")

# 3) 非法 sort_by 必须被工具自己纠正（后端会静默回退到 probability）
r = tools.call_tool("list_customers", {"sort_by": "net_profit", "limit": 2})
d = r.get("result", {})
ok = d.get("sorted_by") == "expected_value"
print(f"  [{'OK' if ok else 'FAIL'}] 非法 sort_by 被纠正为 expected_value（实得 {d.get('sorted_by')}）")
if not ok:
    fails.append("非法 sort_by 未被纠正 —— 会静默按概率排")

# 4) 本地过滤必须标注 post_filtered
r = tools.call_tool("list_customers", {"max_balance": 0, "limit": 3})
d = r.get("result", {})
ok = d.get("post_filtered") is True
print(f"  [{'OK' if ok else 'FAIL'}] 本地过滤标注 post_filtered=True")
if not ok:
    fails.append("本地过滤未标注 post_filtered")

# 5) 阈值工具必须同时给两套阈值并说明区别
r = tools.call_tool("get_model_thresholds", {})
d = r.get("result", {})
for label, ok in [
    ("含 thresholds_grading", isinstance(d.get("thresholds_grading"), dict)),
    ("含 decision_threshold", d.get("decision_threshold") is not None),
    ("含 success_rate", d.get("success_rate") is not None),
    ("含两套阈值区别说明", "不可混用" in (d.get("thresholds_explained") or "")),
]:
    print(f"  [{'OK' if ok else 'FAIL'}] get_model_thresholds {label}")
    if not ok:
        fails.append(f"get_model_thresholds 缺 {label}")

# 6) 写工具：必须返回待确认，且**不含已执行**的迹象
r = tools.call_tool("propose_create_work_order", {"customer_id": "C034525"})
d = r.get("result", {})
for label, ok in [
    ("标记 __pending_action__", d.get("__pending_action__") is True),
    ("requires_confirmation", d.get("requires_confirmation") is True),
    ("带完整 payload", isinstance(d.get("payload"), dict) and d["payload"].get("customer_id")),
    ("payload 含自动生成的 note", bool(d["payload"].get("note"))),
]:
    print(f"  [{'OK' if ok else 'FAIL'}] propose_create_work_order {label}")
    if not ok:
        fails.append(f"propose_create_work_order 缺 {label}")

# 7) 未知工具 / 坏参数
r = tools.call_tool("no_such_tool", {})
print(f"  [{'OK' if not r.get('ok') else 'FAIL'}] 未知工具被拒")
r = tools.call_tool("get_customer_risk", {"wrong_param": 1})
print(f"  [{'OK' if not r.get('ok') else 'FAIL'}] 坏参数被拒（{r.get('error','')[:60]}）")

# 8) 不存在的客户
r = tools.call_tool("get_customer_risk", {"customer_id": "C999999"})
print(f"  [{'OK' if not r.get('ok') else 'FAIL'}] 不存在客户返回失败（{r.get('error','')[:70]}）")

print()
print("=" * 88)
if fails:
    print(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        print(f"  · {f}")
else:
    print("结论：PASS —— 6 个工具均可调用，返回形状统一，写工具不落库")
