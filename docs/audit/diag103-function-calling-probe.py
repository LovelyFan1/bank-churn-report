"""验证：DeepSeek 是否支持任务型 Agent 所需的 function calling。

任务型 Agent 的本质是「LLM 选函数 → 执行 → 用返回值回答」，
所以 function calling 是**硬门槛**，不支持则整个方案不成立。
本脚本用真实的 bank-churn 工具定义测两件事：
  1) 单轮：能否正确选工具并抽对参数
  2) 多轮：能否用工具返回的真实数字组织回答（而不是自己编）
"""
import json
import os
import urllib.request

KEY = os.environ["DEEPSEEK_API_KEY"]
URL = "https://api.deepseek.com/chat/completions"

# ── 工具定义：形状与真实系统端点一致 ──────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_customer_risk",
            "description": "查询单个客户的流失风险详情，返回概率、等级、价值层、期望价值、风险因素与建议动作",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "客户编号，如 C034525"}
                },
                "required": ["customer_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_high_risk_customers",
            "description": "列出高风险客户名单，可按风险等级、价值层筛选并排序",
            "parameters": {
                "type": "object",
                "properties": {
                    "risk_level": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]},
                    "value_tier": {"type": "string", "enum": ["HIGH", "LOW", "ZERO"]},
                    "sort_by": {"type": "string", "enum": ["probability", "expected_value"]},
                    "limit": {"type": "integer", "description": "返回条数，上限 100"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_model_thresholds",
            "description": "获取当前模型的决策阈值、分级线、成本比与挽留成功率",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# 伪造的工具返回（模拟真实接口输出，含关键数字）
FAKE_RESULTS = {
    "list_high_risk_customers": {
        "total": 4821, "returned": 3,
        "items": [
            {"customer_id": "C067546", "probability": 0.9981, "balance": 0,
             "value_tier": "ZERO", "expected_value": 0},
            {"customer_id": "C034525", "probability": 0.9620, "balance": 219490,
             "value_tier": "HIGH", "expected_value": 211140},
            {"customer_id": "C059629", "probability": 0.9987, "balance": 196470,
             "value_tier": "HIGH", "expected_value": 196212},
        ],
    },
    "get_model_thresholds": {
        "decision_threshold": 0.60, "thresholds": {"critical": 0.6825, "high": 0.2468, "medium": 0.0683},
        "cost_ratio": 5.0, "success_rate": 0.30,
        "cost_per_intervention": 10000, "breakeven_expected_value": 33333.33,
    },
    "get_customer_risk": {
        "customer_id": "C059629", "probability": 0.9987, "risk_level": "CRITICAL",
        "value_tier": "HIGH", "balance": 196470, "expected_value": 196212,
        "expected_recoverable": 58864, "individual_net_profit": 48864,
        "risk_factors": ["非活跃用户", "持有 4 个产品（产品超载）", "高龄客户（72 岁）"],
        "action": "客户经理上门 + 定制挽留方案", "channel": "relationship",
    },
}

SYSTEM = (
    "你是银行客户流失预警系统的助手。你可以调用工具查询真实数据。"
    "铁律：回答中出现的每一个数字都必须来自工具返回值，不得自行推算或估计。"
)


def call(messages, tools=None, tool_choice="auto"):
    body = {"model": "deepseek-chat", "messages": messages, "temperature": 0}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
    req = urllib.request.Request(
        URL, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


QUESTIONS = [
    ("极高风险的客户里，哪些值得优先挽回？给我看几个", "应调 list_high_risk_customers"),
    ("C059629 这个人要不要打电话？", "应调 get_customer_risk"),
    ("现在决策阈值是多少？为什么是这个数？", "应调 get_model_thresholds"),
    ("哪个策略挽留效果最好？", "应拒答（by_strategy 只有 1 行）"),
    ("明年的流失趋势会怎样？", "应拒答（客户表无时间字段）"),
]

print("=" * 90)
print("一、单轮：能否正确选工具 + 抽对参数")
print("=" * 90)

ok_tool = 0
for q, expect in QUESTIONS:
    try:
        resp = call([{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": q}], tools=TOOLS)
        msg = resp["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        if tcs:
            picked = tcs[0]["function"]["name"]
            args = tcs[0]["function"]["arguments"]
            print(f"\n  问: {q}")
            print(f"  期望: {expect}")
            print(f"  实际: 调用 {picked}  参数 {args}")
            ok_tool += 1
        else:
            print(f"\n  问: {q}")
            print(f"  期望: {expect}")
            print(f"  实际: **未调工具**，直接回答：")
            print(f"        {(msg.get('content') or '')[:180]}")
    except Exception as e:
        print(f"\n  问: {q}\n  [失败] {type(e).__name__}: {e}")

print(f"\n  调用工具的题数 {ok_tool}/{len(QUESTIONS)}")

print()
print("=" * 90)
print("二、多轮：用工具返回的真实数字组织回答（关键 —— 会不会自己编）")
print("=" * 90)

q = "极高风险的客户里，哪些值得优先挽回？"
messages = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": q}]
resp = call(messages, tools=TOOLS)
msg = resp["choices"][0]["message"]
messages.append(msg)
for tc in msg.get("tool_calls") or []:
    fn = tc["function"]["name"]
    result = FAKE_RESULTS.get(fn, {})
    print(f"  → 执行 {fn}，返回 {json.dumps(result, ensure_ascii=False)[:150]}…")
    messages.append({"role": "tool", "tool_call_id": tc["id"],
                     "content": json.dumps(result, ensure_ascii=False)})

final = call(messages)
answer = final["choices"][0]["message"]["content"]
print()
print("  最终回答：")
print("  " + answer.replace("\n", "\n  "))

print()
print("=" * 90)
print("三、数字保全校验（回答里的数字是否都能在工具返回中找到）")
print("=" * 90)
import re
nums = set(re.findall(r"\d[\d,]*\.?\d*", answer))
allowed = set()
for fn, res in FAKE_RESULTS.items():
    # 收集所有出现在返回值里的数字
    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, (int, float)):
            allowed.add(str(o))
            allowed.add(f"{o:,}")
        elif isinstance(o, str):
            for m in re.findall(r"\d[\d,]*\.?\d*", o):
                allowed.add(m)
    walk(res)

def norm(s):
    return s.replace(",", "").rstrip(".")

allowed_n = {norm(a) for a in allowed}
orphan = sorted(n for n in nums if norm(n) not in allowed_n)
print(f"  回答中数字 {len(nums)} 个：{sorted(nums)}")
print(f"  ⚠ 无法从工具返回溯源的：{orphan if orphan else '无 ✔'}")
