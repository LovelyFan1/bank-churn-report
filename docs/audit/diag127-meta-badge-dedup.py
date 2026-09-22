"""验证三项修复：
  1. 元问题（问系统自身）→ 确定性回答，不再是"当前无可用工具"
  2. 徽章逻辑 → 零工具调用一律不得给 llm_verified（与问题类型无关）
  3. 重复工具调用 → 同一轮内 (工具, 参数) 相同的只执行一次

背景（用户实测 diag126）：
  「可调用工具？」  → 当前无可用工具        （❌ 实际有 9 个）
  「你用的什么技术栈」→ 当前无数据可分析      （❌ 跑偏）
  「你有哪些工具」   → 答对了，但重复调了 12 次工具
  且全部标着「模型作答 · 数字已校验」。

⚠ 只读：不触发任何写确认。
"""
import json
import sys
import urllib.error
import urllib.request
from collections import Counter

sys.path.insert(0, "/app")
BASE = "http://localhost:8000"
fails = []
out = []


def p(s=""):
    out.append(str(s))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=200) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode())


# ══════════════════════════════════════════════════════════
p("=" * 90)
p("零、系统真实能力（用于核对元问题答案是否准确）")
p("=" * 90)
caps = get("/api/agent/capabilities")
real_tools = [t["name"] for t in caps["tools"]]
real_blocked = [b["key"] for b in caps["blocked"]]
p(f"  工具数 {len(real_tools)}: {', '.join(real_tools)}")
p(f"  答不了 {len(real_blocked)} 类: {', '.join(real_blocked)}")

# ══════════════════════════════════════════════════════════
p()
p("=" * 90)
p("一、元问题 → 确定性回答（不走 LLM）")
p("=" * 90)
META_CASES = [
    ("你是谁", "identity"),
    ("你是什么", "identity"),
    ("你能做什么", "ability"),
    ("可调用工具？", "ability"),
    ("你有哪些工具", "ability"),
    ("怎么用你", "ability"),
    ("你用的什么技术栈", "tech"),
    ("你的底层是什么", "tech"),
    ("你是什么模型", "tech"),
    ("今天天气怎么样", "out_of_scope"),
    ("1+1等于几", "out_of_scope"),
    ("讲个笑话", "out_of_scope"),
]
for q, expect_kind in META_CASES:
    code, r = post("/api/agent/ask", {"question": q})
    a = r.get("answer") or {}
    basis = r.get("basis")
    calls = [c["name"] for c in (r.get("tool_calls") or [])]
    mk = a.get("meta_kind")
    ok = code == 200 and basis == "system_meta" and mk == expect_kind and not calls
    if not ok:
        fails.append(f"元问题处理错误：「{q}」basis={basis} meta_kind={mk} calls={calls}")
    p(f"  [{'OK ' if ok else 'FAIL'}] {q}")
    p(f"        basis={basis}  meta_kind={mk}  工具调用={len(calls)}")
    p(f"        结论: {a.get('headline')}")

# ══════════════════════════════════════════════════════════
p()
p("=" * 90)
p("二、能力类元问题的答案必须与真实能力一致（关键：不许说假话）")
p("=" * 90)
code, r = post("/api/agent/ask", {"question": "可调用工具？"})
a = r.get("answer") or {}
facts = a.get("facts") or []
text_all = json.dumps(a, ensure_ascii=False)
p(f"  headline: {a.get('headline')}")
p(f"  facts 条数: {len(facts)}")

# ① 不得再出现"无可用工具/无工具"这类假话
mark = "OK " if "无可用工具" not in text_all else "FAIL"
for bad in ["无可用工具", "没有工具", "无工具", "没有可用工具"]:
    if bad in text_all:
        fails.append(f"元问题答案仍含假话「{bad}」")
p(f"  [{mark}] 不再声称「无可用工具」")

# ② headline 应给出"能查 N 类 / 提议 M 种"，且 N+M 等于真实工具总数。
#   ⚠ 断言不能写死"含数字 9"：答案把工具分成了"查询 6 类 + 操作 3 种"，
#     这比笼统的"9 个工具"更准确。故核对 N+M == len(real_tools)。
import re as _re
m = _re.search(r"能查\s*(\d+)\s*类数据、提议\s*(\d+)\s*种", a.get("headline") or "")
if not m:
    fails.append(f"能力答案 headline 格式不符：{a.get('headline')}")
    p(f"  [FAIL] headline 未给出 N 类/M 种：{a.get('headline')}")
else:
    n_read, n_write = int(m.group(1)), int(m.group(2))
    real_read = len([t for t in caps["tools"] if not t["write"]])
    real_write = len([t for t in caps["tools"] if t["write"]])
    ok = (n_read == real_read and n_write == real_write)
    if not ok:
        fails.append(f"能力数字不符：答案 {n_read}+{n_write}，真实 {real_read}+{real_write}")
    p(f"  [{'OK ' if ok else 'FAIL'}] 查询 {n_read} 类 + 操作 {n_write} 种 "
      f"= {n_read + n_write}（真实 {real_read}+{real_write}={len(real_tools)}）")

# ③ 每个工具名都应出现（作为 note 或 value）
missing = [t for t in real_tools if t not in text_all]
if missing:
    fails.append(f"能力清单漏了工具：{missing}")
p(f"  [{'OK ' if not missing else 'FAIL'}] 9 个工具全部列出"
  + (f"，漏 {missing}" if missing else ""))

# ④ 用户要求：能力清单**只列可做项**，不展示"答不了"。
#    但接口仍保留 blocked 字段（供其它调用方），拦截逻辑也不受影响（下方第五节核验）。
for b in real_blocked:
    if b in text_all:
        fails.append(f"能力清单不应展示答不了的主题 {b}（用户要求只列可做项）")
p(f"  [OK] 能力清单未展示「答不了」条目（{len(real_blocked)} 类仍由 Guard 拦截）")

# ══════════════════════════════════════════════════════════
p()
p("=" * 90)
p("三、徽章逻辑：零工具调用一律不得给 llm_verified")
p("=" * 90)
# 这批问题在修复前都拿到了 llm_verified
NO_TOOL_CASES = [
    "你是谁", "你的底层是什么", "你用的什么技术栈", "可调用工具？",
    "你能做什么", "怎么用你", "今天天气怎么样", "1+1等于几", "讲个笑话",
    "什么是客户流失率",
]
for q in NO_TOOL_CASES:
    code, r = post("/api/agent/ask", {"question": q})
    basis = r.get("basis")
    calls = [c["name"] for c in (r.get("tool_calls") or [])]
    # 无工具调用时，basis 必须是 system_meta / no_data / guard_blocked 之一
    allowed = {"system_meta", "no_data", "guard_blocked"}
    ok = basis in allowed
    if not ok:
        fails.append(f"零工具调用却给了 {basis}：「{q}」")
    p(f"  [{'OK ' if ok else 'FAIL'}] basis={basis:<16} 工具={len(calls)}  「{q}」")

p()
p("  反向核对：调了工具的**业务问题**仍应给 llm_verified")
for q in ["C034525 这个人要不要打电话？", "现在决策阈值是多少？"]:
    code, r = post("/api/agent/ask", {"question": q})
    basis = r.get("basis")
    calls = [c["name"] for c in (r.get("tool_calls") or [])]
    ok = basis == "llm_verified" and calls
    if not ok:
        fails.append(f"业务问题未给 llm_verified：「{q}」basis={basis}")
    p(f"  [{'OK ' if ok else 'FAIL'}] basis={basis:<16} 工具={calls}  「{q}」")

# ══════════════════════════════════════════════════════════
p()
p("=" * 90)
p("四、重复工具调用去重")
p("=" * 90)
code, r = post("/api/agent/ask", {"question": "你有哪些工具"})
calls = [c["name"] for c in (r.get("tool_calls") or [])]
# 该问题现在由 meta 节点回答，应零调用
p(f"  「你有哪些工具」现在由 meta 回答 → 工具调用 {len(calls)} 次")
if calls:
    fails.append(f"元问题仍调用了工具：{calls}")

# 用业务问题验证去重：同一轮内若模型重复发起相同调用，只应执行一次
for q in ["工单处理得怎么样？", "极高风险的客户有哪些"]:
    code, r = post("/api/agent/ask", {"question": q})
    calls = [c["name"] for c in (r.get("tool_calls") or [])]
    c = Counter(calls)
    dupes = {k: v for k, v in c.items() if v > 1}
    ok = not dupes
    if not ok:
        fails.append(f"存在重复工具调用：「{q}」{dupes}")
    p(f"  [{'OK ' if ok else 'FAIL'}] 调用 {calls}")
    if dupes:
        p(f"        重复: {dupes}")

# ══════════════════════════════════════════════════════════
p()
p("=" * 90)
p("五、回归：业务问题不受影响")
p("=" * 90)
REG = [
    ("C034525 这个人要不要打电话？", "customer_detail"),
    ("挽回价值最高的3个客户", "customer_list"),
    ("现在决策阈值是多少？", "facts"),
    ("有哪些工单待处理", "workorder_list"),
]
for q, expect_kind in REG:
    code, r = post("/api/agent/ask", {"question": q})
    a = r.get("answer") or {}
    k = a.get("kind")
    ok = code == 200 and k == expect_kind
    if not ok:
        fails.append(f"回归失败：「{q}」kind={k} 期望 {expect_kind}")
    p(f"  [{'OK ' if ok else 'FAIL'}] kind={k:<16} basis={r.get('basis'):<16} 「{q}」")

# 拒答仍有效
for q in ["真实挽留成功率是多少", "明年的流失趋势怎样", "哪个策略效果最好"]:
    code, r = post("/api/agent/ask", {"question": q})
    ok = r.get("basis") == "guard_blocked"
    if not ok:
        fails.append(f"拒答失效：「{q}」basis={r.get('basis')}")
    p(f"  [{'OK ' if ok else 'FAIL'}] 拒答正常 「{q}」")

p()
p("=" * 90)
if fails:
    p(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        p(f"  · {f}")
else:
    p("结论：PASS —— 元问题确定性回答、徽章判据与问题类型解耦、重复调用去重、业务回归正常")
p("=" * 90)

with open("/tmp/diag127.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("written")
