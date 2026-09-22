"""取证：元问题（问系统自身）为什么答错，且拿到 llm_verified。

用户实测：
  「你是谁」        → 我是银行客户流失预警分析助手   （勉强对）
  「你的底层是什么」 → 我是银行客户流失预警分析助手   （答非所问）
  「你用的什么技术栈」→ 当前无数据可分析              （完全跑偏）
  「可调用工具？」   → 当前无可用工具                 （❌ 实际有 9 个工具）

且**全部**标着"模型作答 · 数字已校验"。

要查清三件事：
  1) 这些问题的 tool_calls 与 basis 实际是什么
  2) 为什么 _needs_data() 没拦住（进而没标 ungrounded）
  3) 「可调用工具」的正确答案系统自己有没有（capabilities 接口）

结论写 JSON，避免控制台编码问题。
"""
import json
import urllib.request

BASE = "http://localhost:8000"
res = {"probes": []}


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


QUESTIONS = [
    "你是谁",
    "你是什么",
    "你的底层是什么",
    "你用的什么技术栈",
    "你是什么模型",
    "可调用工具？",
    "你能做什么",
    "你有哪些工具",
    "怎么用你",
    "今天天气怎么样",
    "1+1等于几",
    "讲个笑话",
]

for q in QUESTIONS:
    d = post("/api/agent/ask", {"question": q})
    a = d.get("answer") or {}
    res["probes"].append({
        "q": q,
        "basis": d.get("basis"),
        "tool_calls": [c["name"] for c in (d.get("tool_calls") or [])],
        "kind": a.get("kind"),
        "headline": a.get("headline"),
        "text": (a.get("text") or "")[:200],
        "n_entities": len(a.get("entities") or []),
        "has_warning": bool(a.get("warning")),
    })

# _needs_data 对这些问题的判定（揭示为什么没标 ungrounded）
import sys  # noqa: E402
sys.path.insert(0, "/app")
from app.agent.graph import _needs_data  # noqa: E402

res["needs_data"] = {q: _needs_data(q) for q in QUESTIONS}

# 系统**自己知道**的元信息（capabilities 接口）
with urllib.request.urlopen(BASE + "/api/agent/capabilities", timeout=60) as r:
    caps = json.loads(r.read().decode())
res["system_knows"] = {
    "tool_count": len(caps.get("tools", [])),
    "tool_names": [t["name"] for t in caps.get("tools", [])],
    "blocked_topics": [b["key"] for b in caps.get("blocked", [])],
}

with open("/tmp/meta_probe.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print("written")
