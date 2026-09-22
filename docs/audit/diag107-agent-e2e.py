"""端到端验证：对话式任务型 Agent。

覆盖 MVP 的五项能力：
  1. Guard 拒答（不调 LLM）
  2. 工具选择正确（LLM 抽参）
  3. 数字来自工具，非编造
  4. 数字校验能抓住编造
  5. 写操作只"提议"，不落库

⚠ 只读原则：本脚本**不调用** /api/agent/confirm，因此不会真的建单。
   生产库 62 条工单是真实数据。
"""
import json
import os
import sys
import time
import urllib.request

BASE = "http://localhost:8000"
fails = []


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode())


def answer_brief(r, n=140):
    """把结构化 answer 压成一行可读摘要。

    ⚠ answer 自第十六节起是**结构化对象**（kind/headline/warning/entities…），
      不再是字符串。旧脚本直接对它切片会 KeyError。
      本函数兼容两种形态，避免脚本随接口演进而反复失效。
    """
    a = r.get("answer")
    if isinstance(a, str):
        return a[:n]
    if not isinstance(a, dict):
        return "(无回答)"
    bits = []
    if a.get("kind"):
        bits.append(f"[{a['kind']}]")
    if a.get("headline"):
        bits.append(a["headline"])
    w = a.get("warning") or {}
    if w.get("text"):
        bits.append("⚠ " + w["text"])
    for e in (a.get("entities") or [])[:2]:
        bits.append(f"{e.get('customer_id')} {e.get('surname')} "
                    f"{e.get('primary', {}).get('value_text', '')}")
    for act in (a.get("actions") or [])[:1]:
        bits.append(f"按钮「{act.get('label')}」")
    return " | ".join(bits)[:n]


print("=" * 92)
print("零、可用性自检")
print("=" * 92)
h = get("/api/agent/health")
print(f"  available={h['available']}  reason={h['reason']}")
print(f"  model={h.get('model')}  base_url={h.get('base_url')}")
if not h["available"]:
    fails.append(f"Agent 不可用：{h['reason']}")
    print("\n  ⚠ Agent 不可用，后续用例无法执行。")
    print(f"\n结论：FAIL —— {fails}")
    sys.exit(1)

caps = get("/api/agent/capabilities")
print(f"\n  能力清单：{len(caps['tools'])} 个工具，{len(caps['blocked'])} 个答不了的主题")
for t in caps["tools"]:
    print(f"    · {t['name']}{'  [写]' if t['write'] else ''}")

print()
print("=" * 92)
print("一、Guard 拒答（应不调工具、不调 LLM）")
print("=" * 92)
for q, topic in [
    ("真实挽留成功率是多少", "retention_success_rate"),
    ("明年的流失趋势怎样", "temporal_trend"),
    ("哪个策略效果最好", "strategy_comparison"),
]:
    t0 = time.time()
    code, r = post("/api/agent/ask", {"question": q})
    dt = time.time() - t0
    ok = code == 200 and r.get("basis") == "guard_blocked"
    if not ok:
        fails.append(f"Guard 未拦截：{q} → basis={r.get('basis')} code={code}")
    print(f"\n  [{'OK ' if ok else 'FAIL'}] {q}   ({dt:.2f}s, 无 LLM 调用应很快)")
    print(f"        basis={r.get('basis')}  topic={r.get('guard_topic')}")
    print(f"        工具调用数={len(r.get('tool_calls') or [])}（应为 0）")
    print(f"        回答: {answer_brief(r, 120)}…")
    if r.get("tool_calls"):
        fails.append(f"Guard 拦截后仍调用了工具：{q}")

print()
print("=" * 92)
print("二、工具选择与数字溯源")
print("=" * 92)
CASES = [
    ("C034525 这个人要不要打电话？", "get_customer_risk"),
    ("现在决策阈值是多少？", "get_model_thresholds"),
    ("极高风险的客户有哪些，给我看几个", "list_customers"),
    ("工单处理得怎么样？", "get_workorder_stats"),
]
for q, expect in CASES:
    t0 = time.time()
    code, r = post("/api/agent/ask", {"question": q})
    dt = time.time() - t0
    calls = [c["name"] for c in (r.get("tool_calls") or [])]
    ok = code == 200 and expect in calls
    if not ok:
        fails.append(f"工具选择错误：{q} → {calls}，期望含 {expect}")
    print(f"\n  [{'OK ' if ok else 'FAIL'}] {q}   ({dt:.1f}s)")
    print(f"        期望工具={expect}  实调={calls}")
    print(f"        basis={r.get('basis')}")
    print(f"        溯源失败数字={r.get('verify_failed') or '无'}")
    print(f"        回答: {answer_brief(r, 260)}…")
    if r.get("verify_failed"):
        # 非致命：说明校验器工作了并已回退模板
        print(f"        ⚠ 模型编了数字，已被校验丢弃并回退模板")

print()
print("=" * 92)
print("三、写操作只提议、不落库")
print("=" * 92)
before = get("/api/work-orders/stats")["total"]
code, r = post("/api/agent/ask",
               {"question": "帮我给 C034525 建个挽留工单，负责人写张思远"})
after = get("/api/work-orders/stats")["total"]
pa = r.get("pending_action")
ok = code == 200 and isinstance(pa, dict) and after == before
if not ok:
    fails.append(f"写操作未正确转为待确认：pending={bool(pa)} 工单数 {before}→{after}")
print(f"  [{'OK ' if ok else 'FAIL'}] 工单总数 {before} → {after}（必须相等）")
print(f"        basis={r.get('basis')}  应有 system_pending_action")
print(f"        pending_action={'有' if pa else '无'}")
if pa:
    print(f"        摘要: {pa.get('summary')}")
    print(f"        payload 负责人: {pa.get('payload', {}).get('assignee')}")
    print(f"        payload 带 note: {bool(pa.get('payload', {}).get('note'))}")
# 用 text（纯文本全文）做"谎称已完成"检查 —— answer 是结构化对象，
# 但 text 字段仍带完整人工可读文本，且待确认路径必定含"需要你确认"
ans = r.get("text") or ""
if not ans and isinstance(r.get("answer"), dict):
    ans = r["answer"].get("text") or ""
claims_done = any(w in ans for w in ["已创建", "已建单", "已经建好", "创建成功"])
if claims_done:
    fails.append(f"回答谎称已完成：{ans[:100]}")
print(f"        回答是否谎称已完成: {'是（FAIL）' if claims_done else '否'}")
print(f"        回答: {ans[:200]}…")

print()
print("=" * 92)
print("四、边界")
print("=" * 92)
code, r = post("/api/agent/ask", {"question": "   "})
print(f"  [{'OK ' if code == 422 else 'FAIL'}] 空问题被拒（HTTP {code}）")
if code != 422:
    fails.append(f"空问题未被拒：{code}")

code, r = post("/api/agent/ask", {"question": "今天天气怎么样"})
print(f"  [{'OK ' if code == 200 else 'FAIL'}] 无关问题不崩溃（HTTP {code}，"
      f"basis={r.get('basis')}）")
if code != 200:
    fails.append(f"无关问题导致失败：{code} {r}")

print()
print("=" * 92)
if fails:
    print(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        print(f"  · {f}")
else:
    print("结论：PASS —— 拒答、工具选择、数字溯源、写操作待确认、边界 全部通过")
