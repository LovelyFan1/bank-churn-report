"""核查批量测试暴露的两个问题。

问题 A（用例 10）：问「挽留成功率这个假设是多少」被 Guard 拦了。
  但这个问题的答案是**已知的**（risk-info 里 success_rate=0.3, 
  source=assumption）。用户问的是"假设值是多少"，不是"真实挽留率"。
  → 疑似**过度拦截**（Guard 误伤）。

问题 B（用例 21）：问「帮我取消已建单待处理状态客户」只调了
  list_work_orders，basis=llm_verified，**没有产生待确认动作**。
  而 diag117 当时是通过的。需要确认：是否因为库里已无 pending 工单？

同时核对数据库现状（用例 15/16 显示"共 62 单""无待处理工单"，
而批量测试前是 63 单 / pending=1）。
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
    res["counts"] = {st: n for st, n in rows}
    res["total"] = sum(n for _, n in rows)
    res["ids_by_status"] = {}
    for o in db.query(WorkOrder).order_by(WorkOrder.id).all():
        res["ids_by_status"].setdefault(o.status, []).append(
            {"id": o.id, "cid": o.customer_id, "name": o.customer_name})
finally:
    db.close()


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


# 问题 A：三个变体的拦截情况
res["guard_probe"] = {}
for q in [
    "挽留成功率这个假设是多少",
    "挽留成功率是多少",
    "真实挽留成功率是多少",
    "成本模型假设的挽留成功率是多少",
    "RETENTION_SUCCESS_RATE 是多少",
]:
    d = post("/api/agent/ask", {"question": q})
    a = d.get("answer") or {}
    res["guard_probe"][q] = {
        "basis": d.get("basis"),
        "topic": d.get("guard_topic"),
        "calls": [c["name"] for c in (d.get("tool_calls") or [])],
        "headline": a.get("headline"),
        "kind": a.get("kind"),
    }

# 问题 B：取消请求的完整返回
d = post("/api/agent/ask", {"question": "帮我取消已建单待处理状态客户"})
a = d.get("answer") or {}
res["cancel_probe"] = {
    "basis": d.get("basis"),
    "calls": [c["name"] for c in (d.get("tool_calls") or [])],
    "has_pending": bool(d.get("pending_action")),
    "headline": a.get("headline"),
    "text": (a.get("text") or "")[:300],
}

with open("/tmp/probe.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
print("written")
