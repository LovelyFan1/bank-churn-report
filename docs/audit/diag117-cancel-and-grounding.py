"""验证：取消工单流程 + 无依据作答拦截 + 新工具。

⚠ 数据安全：本脚本**不执行任何删除**。它只验证"提议"是否正确生成，
   以及 HTTP 取出的工单是否原样存在。用户手工建的 #63 不得被动到。

覆盖三个缺陷（全部实测发现）：
  1. 问「取消待处理工单」时模型不调工具就答"没有" → 现在必须查到并提议
  2. 无工具调用却拿 `llm_verified` 徽章 → 现在标 `ungrounded`
  3. 缺 list_work_orders / 取消类工具 → 现已补齐
"""
import io
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
fails = []


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


out = []
def p(s=""):
    out.append(str(s))


p("=" * 88)
p("零、前提：库中现有 pending 工单")
p("=" * 88)
stats = get("/api/work-orders/stats")
p(f"  stats: {json.dumps(stats, ensure_ascii=True)}")
pending_before = stats["pending"]
lst = get("/api/work-orders?status=pending&page=1&page_size=20")
pending_ids = [o["id"] for o in lst["items"]]
p(f"  pending 工单 id: {pending_ids}")
if pending_before == 0:
    p("  ⚠ 当前无 pending 工单，取消类断言将跳过")

p()
p("=" * 88)
p("一、问「帮我取消已建单待处理状态客户」")
p("=" * 88)
code, r = post("/api/agent/ask", {"question": "帮我取消已建单待处理状态客户"})
p(f"  HTTP {code}  basis={r.get('basis')}")
calls = [c["name"] for c in (r.get("tool_calls") or [])]
p(f"  tool_calls = {calls}")
a = r.get("answer") or {}
p(f"  kind     = {a.get('kind')}")
p(f"  headline = {a.get('headline')}")
pa = r.get("pending_action")
p(f"  pending_action = {'有' if pa else '无'}")
if pa:
    p(f"    action  = {pa.get('action')}")
    p(f"    summary = {pa.get('summary')}")
    p(f"    payload = {json.dumps(pa.get('payload'), ensure_ascii=True)}")

if pending_before > 0:
    if "list_work_orders" not in calls:
        fails.append("未调用 list_work_orders 查具体工单")
    if not pa:
        fails.append("未生成待确认的取消动作（用户看不到按钮）")
    elif pa.get("action") != "delete_work_order":
        fails.append(f"提议的 action 应为 delete_work_order，实得 {pa.get('action')}")
    else:
        oid = (pa.get("payload") or {}).get("order_id")
        if oid not in pending_ids:
            fails.append(f"提议删除的 #{oid} 不在 pending 列表 {pending_ids} 中")
    # 必须不是"凭记忆作答"
    if r.get("basis") == "ungrounded":
        fails.append("仍在无依据作答")
    if not calls:
        fails.append("tool_calls 为空 —— 又是凭记忆作答")

p()
p("=" * 88)
p("二、关键：没有真正删除（#63 等工单必须原样存在）")
p("=" * 88)
stats_after = get("/api/work-orders/stats")
p(f"  pending: {pending_before} → {stats_after['pending']}")
p(f"  total  : {stats['total']} → {stats_after['total']}")
if stats_after["pending"] != pending_before or stats_after["total"] != stats["total"]:
    fails.append("提议阶段竟然改了数据库（写操作未隔离）")
else:
    p("  [OK] 提议阶段零写入")

p()
p("=" * 88)
p("三、ungrounded 拦截：需要数据但不查数据的问题")
p("=" * 88)
# 「工单」属于需要数据的词。模型若正常会去查；这里验证的是**兜底机制**：
# 若它真的没查，basis 必须是 ungrounded 且带显著警示，绝不是 llm_verified。
code, r2 = post("/api/agent/ask", {"question": "现在工单是什么情况"})
calls2 = [c["name"] for c in (r2.get("tool_calls") or [])]
p(f"  basis={r2.get('basis')}  tool_calls={calls2}")
a2 = r2.get("answer") or {}
w2 = a2.get("warning") or {}
p(f"  warning={json.dumps(w2, ensure_ascii=True)[:160]}")
if not calls2:
    if r2.get("basis") != "ungrounded":
        fails.append(f"无工具调用却未标 ungrounded（basis={r2.get('basis')}）")
    if w2.get("level") != "guard":
        fails.append("ungrounded 未给出显著警示")
else:
    p("  （模型调了工具，属正常路径；ungrounded 兜底未被触发）")

p()
p("=" * 88)
p("四、新工具：list_work_orders 各状态可用")
p("=" * 88)
for st in ["pending", "in_progress", "completed", "lost", None]:
    q = f"/api/work-orders?page=1&page_size=5" + (f"&status={st}" if st else "")
    d = get(q)
    p(f"  status={str(st):<12} returned={len(d['items']):<3} total={d['total']}")
# 直接测工具函数
import sys  # noqa: E402
sys.path.insert(0, "/app")
from app.agent import tools as T  # noqa: E402
rr = T.call_tool("list_work_orders", {"status": "pending", "limit": 5})
p(f"  工具直调 list_work_orders(pending): ok={rr.get('ok')} "
  f"returned={(rr.get('result') or {}).get('returned')}")
if not rr.get("ok"):
    fails.append(f"list_work_orders 工具调用失败：{rr.get('error')}")
else:
    items = (rr["result"] or {}).get("items") or []
    if items and "order_id" not in items[0]:
        fails.append("list_work_orders 未返回 order_id 字段")

p()
p("=" * 88)
p("五、提议类工具存在且不落库")
p("=" * 88)
for name, args in [("propose_delete_work_order",
                    {"order_id": pending_ids[0] if pending_ids else 1}),
                   ("propose_update_work_order",
                    {"order_id": pending_ids[0] if pending_ids else 1,
                     "status": "in_progress"})]:
    r3 = T.call_tool(name, args)
    ok = r3.get("ok")
    res = r3.get("result") or {}
    p(f"  [{'OK ' if ok else 'FAIL'}] {name} → "
      f"pending={res.get('__pending_action__')} action={res.get('action')}")
    if not ok:
        fails.append(f"{name} 调用失败：{r3.get('error')}")
    elif not res.get("__pending_action__"):
        fails.append(f"{name} 未标记为待确认动作")

p()
p("=" * 88)
p("六、最终确认：数据库未被本测试改动")
p("=" * 88)
final = get("/api/work-orders/stats")
p(f"  pending={final['pending']} total={final['total']}")
if final["pending"] != pending_before or final["total"] != stats["total"]:
    fails.append("测试结束后数据被改动")
else:
    p("  [OK] 数据与测试开始时一致")

p()
p("=" * 88)
if fails:
    p(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        p(f"  · {f}")
else:
    p("结论：PASS —— 取消流程正确提议、零写入、ungrounded 兜底就绪、新工具可用")
p("=" * 88)

io.open("/tmp/diag117.txt", "w", encoding="utf-8").write("\n".join(out))
print("written")
