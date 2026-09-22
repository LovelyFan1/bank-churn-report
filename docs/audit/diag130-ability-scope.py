"""验证：能力范围只列 9 个可做项，不含"答不了"。

用户要求：
  · 能力范围不要有「3 类问题答不了」
  · 只显示 9 个可查/可操作项，且文案为指定样式

同时核对：**拦截逻辑不受影响** —— 直接问那三类问题仍应被拒答，
只是不再把"答不了什么"摆在能力面板上。
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/app")
BASE = "http://localhost:8000"
fails = []
out = []


def p(s=""):
    out.append(str(s))


def get(p_):
    with urllib.request.urlopen(BASE + p_, timeout=60) as r:
        return json.loads(r.read().decode())


def post(p_, body):
    req = urllib.request.Request(
        BASE + p_, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=200) as r:
        return json.loads(r.read().decode())


# ══════════════════════════════════════════════════════════
p("=" * 88)
p("一、capabilities 接口：9 个可做项，文案齐全")
p("=" * 88)
caps = get("/api/agent/capabilities")
tools = caps["tools"]
p(f"  工具数: {len(tools)}")
if len(tools) != 9:
    fails.append(f"工具数应为 9，实得 {len(tools)}")

EXPECT = {
    "get_customer_risk": "查单个客户的流失风险详情",
    "list_customers": "按条件查客户名单",
    "get_model_thresholds": "查当前模型的口径：决策阈值、分位数分级线、成本比、挽留成功率",
    "get_business_summary": "查成本收益推算：年度流失、期望挽留人数、干预投入、ROI",
    "get_workorder_stats": "查工单统计：各状态数量、负责人清单",
    "list_work_orders": "列出具体工单（含编号、客户、负责人、状态）",
    "propose_create_work_order": "提议给某个客户创建挽留工单（不会立即执行）",
    "propose_update_work_order": "提议修改工单（改状态 / 负责人 / 备注），不会立即执行",
    "propose_delete_work_order": "提议删除工单（即「取消这张单」），不会立即执行",
}
for t in tools:
    name, label, write = t["name"], t.get("label"), t["write"]
    want = EXPECT.get(name)
    ok = label == want
    if not ok:
        fails.append(f"{name} 文案不符\n      实得: {label}\n      期望: {want}")
    p(f"  [{'OK ' if ok else 'FAIL'}] {'需确认' if write else '查询'}  {name}")
    p(f"        {label}")

# 每个工具都必须有 label（否则前端会退化去显示 docstring 首行）
no_label = [t["name"] for t in tools if not t.get("label")]
if no_label:
    fails.append(f"以下工具缺 label：{no_label}")
p(f"  [{'OK ' if not no_label else 'FAIL'}] 9 个工具均有 label")

# ══════════════════════════════════════════════════════════
p()
p("=" * 88)
p("二、能力类提问（meta 节点）也只列可做项")
p("=" * 88)
r = post("/api/agent/ask", {"question": "可调用工具？"})
a = r["answer"]
facts = a.get("facts") or []
text_all = json.dumps(a, ensure_ascii=False)
p(f"  headline: {a.get('headline')}")
p(f"  facts 条数: {len(facts)}（应为 9）")
if len(facts) != 9:
    fails.append(f"能力 facts 应为 9 条，实得 {len(facts)}")

# 不得出现"答不了"类条目
bad_labels = [f for f in facts if "答不了" in str(f.get("label", ""))]
if bad_labels:
    fails.append(f"能力清单仍含「答不了」条目：{bad_labels}")
p(f"  [{'OK ' if not bad_labels else 'FAIL'}] 无「答不了」条目")

for bad in ["我答不了", "另有 3 类", "哪类问题", "样本仅 1 组", "无真实回访数据"]:
    if bad in text_all:
        fails.append(f"能力答案仍含「{bad}」")
p(f"  [{'OK ' if not any(b in text_all for b in ['我答不了', '另有 3 类']) else 'FAIL'}] "
  f"warning 不再提「答不了」")

# 9 个工具名都要出现
from app.agent import tools as T  # noqa: E402
missing = [n for n in T.TOOL_SPECS if n not in text_all]
if missing:
    fails.append(f"能力清单漏了工具：{missing}")
p(f"  [{'OK ' if not missing else 'FAIL'}] 9 个工具名全部出现")

p()
p("  逐条核对（应与你给的样式一致）:")
for f in facts:
    p(f"    [{f['label']}] {f['value']}")

# ══════════════════════════════════════════════════════════
p()
p("=" * 88)
p("三、关键：拦截逻辑不受影响（只是不再展示）")
p("=" * 88)
for q, topic in [
    ("真实挽留成功率是多少", "retention_success_rate"),
    ("明年的流失趋势怎样", "temporal_trend"),
    ("哪个策略效果最好", "strategy_comparison"),
]:
    d = post("/api/agent/ask", {"question": q})
    ok = d.get("basis") == "guard_blocked" and d.get("guard_topic") == topic
    if not ok:
        fails.append(f"拒答失效：「{q}」basis={d.get('basis')} topic={d.get('guard_topic')}")
    p(f"  [{'OK ' if ok else 'FAIL'}] {q} → {d.get('basis')} / {d.get('guard_topic')}")

# 接口仍保留 blocked（供其它调用方与测试），只是前端不展示
p()
p(f"  接口仍保留 blocked 字段: {len(caps.get('blocked') or [])} 条"
  f"（前端不再展示，但拦截与其它调用方不受影响）")
if not caps.get("blocked"):
    fails.append("capabilities 丢了 blocked 字段（其它调用方可能依赖）")

p()
p("=" * 88)
if fails:
    p(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        p(f"  · {f}")
else:
    p("结论：PASS —— 能力范围只列 9 个可做项、文案一致、拦截逻辑不受影响")
p("=" * 88)

with open("/tmp/diag130.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("written")
