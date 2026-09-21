"""评估：现有接口是否足以支撑智能体（Agent）工具调用。

智能体的本质是「LLM 选工具 → 调接口 → 用返回值组织回答」。
因此接口层需要满足三个条件：
  1) 有**可被语义检索**的接口（按客户/按策略/按指标）
  2) 返回体**自带口径**（否则智能体无法正确解释数字）
  3) 关键接口**足够快**（LLM 一轮可能调多个）

本脚本逐条核查这三点，不凭印象。
"""
import json
import time
import urllib.request

BASE = "http://localhost:8000"


def call(path, timeout=300):
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode()), time.time() - t0
    except Exception as e:
        return None, {"err": f"{type(e).__name__}: {e}"}, time.time() - t0


_, spec, _ = call("/openapi.json")
paths = spec["paths"]
gets = [p for p, m in paths.items() if "get" in m]
posts = [p for p, m in paths.items() if "post" in m]

print("=" * 78)
print("一、接口盘点")
print("=" * 78)
print(f"  总计 {len(paths)} 个路径   GET {len(gets)}   POST {len(posts)}")
print()
print("  面向智能体最有用的「查询类」接口：")
candidates = [
    ("/api/customers/{customer_id}", "按客户编号取单个客户（含风险/策略/理由）"),
    ("/api/customers", "按条件筛选客户列表"),
    ("/api/eda/key-insights", "关键发现（自然语言短语）"),
    ("/api/eda/churn-analysis", "流失分析"),
    ("/api/cost-benefit/retention-summary", "挽留战报（含口径说明）"),
    ("/api/portfolio/matrix", "价值层×风险等级矩阵"),
    ("/api/model/risk-info", "阈值与决策指标"),
    ("/api/work-orders/{order_id}", "工单详情"),
    ("/api/work-orders", "工单列表"),
    ("/api/dashboard/summary", "首屏聚合"),
]
for p, desc in candidates:
    ok = "OK " if p in paths else "-- "
    print(f"    [{ok}] {p:<38} {desc}")

print()
print("=" * 78)
print("二、返回体是否自带口径（智能体能否正确解释数字）")
print("=" * 78)

checks = [
    ("/api/model/risk-info", ["thresholds", "decision_threshold",
                              "decision_metrics", "success_rate", "cost_ratio"]),
    ("/api/cost-benefit/summary", ["basis", "success_rate", "note",
                                   "tp_at_threshold", "expected_retained"]),
    ("/api/cost-benefit/retention-summary", ["basis", "roi_basis", "note",
                                             "success_rate"]),
    ("/api/portfolio/matrix", ["basis", "thresholds", "note", "cells"]),
    ("/api/dashboard/summary", ["business_summary", "risk_info", "overview"]),
]

for path, keys in checks:
    code, d, dt = call(path)
    if code != 200:
        print(f"  [FAIL] {path} HTTP {code}")
        continue
    present = [k for k in keys if k in d]
    missing = [k for k in keys if k not in d]
    mark = "OK " if not missing else "!! "
    print(f"  [{mark}] {path}")
    print(f"         已有口径字段 {len(present)}/{len(keys)}"
          + (f"   缺: {missing}" if missing else "   （齐全）"))

print()
print("=" * 78)
print("三、调用延迟（智能体一轮可能并发多个）")
print("=" * 78)
lat = []
for p in ["/api/customers/C000001", "/api/model/risk-info",
          "/api/cost-benefit/summary", "/api/portfolio/matrix",
          "/api/work-orders/1", "/api/eda/key-insights",
          "/api/dashboard/summary", "/api/customers?page=1&page_size=20"]:
    code, _, dt = call(p)
    lat.append((p, code, dt))
    print(f"    {dt:6.2f}s  [{code}]  {p}")
print()
worst = max(l[2] for l in lat)
print(f"  最慢 {worst:.2f}s —— 若智能体一轮调 3 个，用户等 {worst*3:.1f}s")

print()
print("=" * 78)
print("四、智能体所需但目前缺失的能力")
print("=" * 78)
missing_features = [
    ("对话接口", "/api/agent/chat", "不存在 —— 前端 Assistant.vue 是空占位页"),
    ("客户搜索（按姓名）", "/api/customers?search=xx", "需确认是否支持"),
    ("策略清单", "/api/intervention/strategies", "不存在（策略在 recommend_action 里）"),
    ("自然语言→筛选条件", "—", "无：客户列表只接受结构化 query"),
    ("知识库/文档检索", "—", "无：口径说明只存在于代码注释与报告"),
]
for name, ep, note in missing_features:
    print(f"  · {name:<20} {ep:<34} {note}")

# 验证客户搜索是否可用
code, d, _ = call("/api/customers?search=Bentley&page=1&page_size=5")
n = d.get("total") if isinstance(d, dict) else None
print()
print(f"  实测 /api/customers?search=Bentley → total={n}")
