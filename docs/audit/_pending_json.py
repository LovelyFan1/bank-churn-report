"""核查（JSON 输出，避免控制台编码问题）：pending 工单实况 + Agent 能力缺口。

用户反馈两点：
  1) 问「帮我取消已建单待处理状态客户」，Agent 答「没有已建单待处理状态的客户」
     —— 用户认为确实有待处理工单
  2) 切换窗口时会话消失，希望有本地缓存

本脚本只查第 1 点的**事实**：库里到底有没有 pending 工单。
结论写入 JSON 文件，由宿主机 docker cp 取出，不经控制台。
"""
import json
import urllib.request

from sqlalchemy import func

from app.database import SessionLocal
from app.models.work_order import WorkOrder

BASE = "http://localhost:8000"
res = {}

db = SessionLocal()
try:
    rows = (db.query(WorkOrder.status, func.count(WorkOrder.id))
            .group_by(WorkOrder.status).all())
    res["counts_by_status"] = {st: n for st, n in rows}
    res["total"] = sum(n for _, n in rows)

    pend = (db.query(WorkOrder).filter(WorkOrder.status == "pending")
            .order_by(WorkOrder.id).all())
    res["pending_orders"] = [
        {"id": o.id, "customer_id": o.customer_id,
         "customer_name": o.customer_name, "assignee": o.assignee,
         "created_at": str(o.created_at)}
        for o in pend
    ]

    recent = (db.query(WorkOrder).order_by(WorkOrder.id.desc()).limit(15).all())
    res["recent_orders"] = [
        {"id": o.id, "status": o.status, "customer_id": o.customer_id,
         "customer_name": o.customer_name, "assignee": o.assignee,
         "created_at": str(o.created_at)}
        for o in recent
    ]
finally:
    db.close()

with urllib.request.urlopen(BASE + "/api/work-orders/stats", timeout=60) as r:
    res["stats_api"] = json.loads(r.read().decode())

body = json.dumps({"question": "帮我取消已建单待处理状态客户"}).encode()
req = urllib.request.Request(BASE + "/api/agent/ask", data=body,
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode())
a = d.get("answer") or {}
res["agent_reply"] = {
    "basis": d.get("basis"),
    "tool_calls": d.get("tool_calls"),
    "kind": a.get("kind"),
    "headline": a.get("headline"),
    "text": (a.get("text") or "")[:300],
}

from app.agent import tools as T  # noqa: E402
res["agent_tools"] = {"read": T.READ_TOOL_NAMES, "write": T.WRITE_TOOL_NAMES}

with open("/tmp/facts.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print("written")
