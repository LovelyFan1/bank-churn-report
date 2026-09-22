"""核查：是否真的没有"待处理(pending)"工单，以及 Agent 为何那样回答。

用户反馈：问「帮我取消已建单待处理状态客户」，Agent 答
「没有已建单待处理状态的客户」，但用户认为确实有待处理的工单。

必须查清三件事（不能拿 Agent 的答复当结论）：
  1) 数据库里到底有没有 pending 工单 —— 直接查库，不信 stats 接口
  2) stats 接口与直接查库是否一致
  3) Agent 有没有"取消工单"的能力 —— 若没有，它注定答不了这个问题，
     却给了肯定式否定（"没有"），这属于「用不存在的能力作答」

⚠ 输出写文件，避免 GBK 控制台丢字。
"""
import io
import json
import urllib.request

from sqlalchemy import func

from app.database import SessionLocal
from app.models.work_order import WorkOrder

BASE = "http://localhost:8000"
out = []


def p(s=""):
    out.append(str(s))


db = SessionLocal()
try:
    p("=" * 84)
    p("一、直接查库：各状态工单数")
    p("=" * 84)
    rows = (db.query(WorkOrder.status, func.count(WorkOrder.id))
            .group_by(WorkOrder.status).all())
    total = 0
    for st, n in rows:
        p(f"  {st:<14} {n}")
        total += n
    p(f"  {'合计':<14} {total}")

    p()
    p("=" * 84)
    p("二、pending 工单明细（若有）")
    p("=" * 84)
    pend = (db.query(WorkOrder).filter(WorkOrder.status == "pending")
            .order_by(WorkOrder.created_at.desc()).all())
    if not pend:
        p("  （无 pending 工单）")
    for o in pend:
        p(f"  #{o.id} {o.customer_id} {o.customer_name} "
          f"assignee={o.assignee} created={o.created_at}")

    p()
    p("=" * 84)
    p("三、最近 12 张工单（看是否刚建过单）")
    p("=" * 84)
    recent = (db.query(WorkOrder).order_by(WorkOrder.created_at.desc())
              .limit(12).all())
    for o in recent:
        p(f"  #{o.id:<4} {o.status:<12} {o.customer_id} {o.customer_name:<12} "
          f"assignee={o.assignee or '-'} created={o.created_at}")
finally:
    db.close()

p()
p("=" * 84)
p("四、stats 接口 vs 直接查库")
p("=" * 84)
with urllib.request.urlopen(BASE + "/api/work-orders/stats", timeout=60) as r:
    st = json.loads(r.read().decode())
p(f"  stats 接口: {json.dumps(st, ensure_ascii=True)}")

p()
p("=" * 84)
p("五、Agent 对「取消待处理工单」的回答与工具调用")
p("=" * 84)
body = json.dumps({"question": "帮我取消已建单待处理状态客户"}).encode()
req = urllib.request.Request(BASE + "/api/agent/ask", data=body,
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode())
p(f"  basis      = {d.get('basis')}")
p(f"  tool_calls = {json.dumps(d.get('tool_calls'), ensure_ascii=True)}")
a = d.get("answer") or {}
p(f"  kind       = {a.get('kind')}")
p(f"  headline   = {json.dumps(a.get('headline'), ensure_ascii=True)}")
p(f"  text       = {json.dumps((a.get('text') or '')[:400], ensure_ascii=True)}")

p()
p("=" * 84)
p("六、Agent 现有能力清单（有无取消工单的工具）")
p("=" * 84)
from app.agent import tools as T  # noqa: E402

p(f"  只读: {T.READ_TOOL_NAMES}")
p(f"  写  : {T.WRITE_TOOL_NAMES}")
has_cancel = any("cancel" in n or "delete" in n for n in T.TOOL_SPECS)
p(f"  有取消/删除类工具: {has_cancel}")

io.open("/tmp/pending_facts.txt", "w", encoding="utf-8").write("\n".join(out))
print("written /tmp/pending_facts.txt")
