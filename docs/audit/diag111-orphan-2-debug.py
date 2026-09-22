"""诊断：为什么「帮我调出挽回价值最高的3个客户」被判数字不可溯源。

用户实测现象：回答被丢弃、回退模板，提示「无法溯源的数字（2）」。
即工具返回值里找不到数字 2 —— 需要确认 2 从哪来。

可能来源：
  a) LLM 自己数了"表中第 2 行"之类
  b) 工具返回的 JSON 里确实有个 2，但被 _collect_numbers 漏了
  c) 回答里的中文数字"二"被误判
  d) 列表序号没被剥离干净

本脚本打印原始回答与校验细节，定位真实原因。
"""
import json
import sys

sys.path.insert(0, "/app")
from app.agent import graph as G  # noqa: E402
from app.agent import verify as V  # noqa: E402
from app.agent.state import initial_state  # noqa: E402

Q = "帮我调出挽回价值最高的3个客户"

print("=" * 88)
print(f"问题：{Q}")
print("=" * 88)

st = initial_state("diag", Q)
out = G.get_graph().invoke(st, {"recursion_limit": 20})

print(f"\nbasis      = {out.get('answer_basis')}")
print(f"verify_fail= {out.get('verify_fail')}")
print(f"工具调用   = {[(r['name'], r['args']) for r in out.get('tool_calls_made', [])]}")

print("\n" + "=" * 88)
print("模型原始草稿（draft）——即被丢弃的那段")
print("=" * 88)
print(out.get("draft", "(空)"))

print("\n" + "=" * 88)
print("工具返回数据里的全部数字")
print("=" * 88)
src = set()
V._collect_numbers(out.get("tool_results", []), src)
print(f"  共 {len(src)} 个：{sorted(src)}")

print("\n" + "=" * 88)
print("白名单扩张后是否含 2.0")
print("=" * 88)
allowed = V._expand_allowed(src)
print(f"  白名单大小 {len(allowed)}")
print(f"  含 2.0 ? {2.0 in allowed}")
print(f"  含 1.0 ? {1.0 in allowed}")
print(f"  含 3.0 ? {3.0 in allowed}")

print("\n" + "=" * 88)
print("逐个数字复查（找出被判孤儿的）")
print("=" * 88)
ok, orphans = V.verify(out.get("draft", ""), out.get("tool_results", []))
print(f"  结论 passed={ok}  orphans={orphans}")

# 打印草稿里抽取到的所有数字及其判定
import re
text = V._strip_ordinals(out.get("draft", ""))
for raw in V._NUM_RE.findall(text):
    try:
        n = float(raw.replace(",", ""))
    except ValueError:
        continue
    found = V._found_in_registry(n, allowed)
    flag = "OK " if found else "孤儿"
    if not found:
        print(f"    [{flag}] {raw!r} → {n}")
