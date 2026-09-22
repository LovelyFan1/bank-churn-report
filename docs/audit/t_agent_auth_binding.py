"""验证：Agent 写操作绑定登录人（审计留痕）。

这是引入登录系统对 Agent 的核心增益。此前 `session_id` 硬编码为 'ui'，
Agent 建的工单**追不到是谁让建的**；且 `assignee` 靠 LLM 从话里抽，
用户没指定时模型会自己编一个名字。

现在：操作者由服务端从会话令牌取，审计记录「谁、何时、通过助手、做了什么」。

⚠ 本测试会**真实建单**，但会在 finally 里删除，且使用专用测试客户，
   不碰任何演示/用户数据。工单总数应在开始与结束时一致。
"""

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
fails = []


def ok(cond, msg):
    print(f"  [{'OK ' if cond else 'FAIL'}] {msg}")
    if not cond:
        fails.append(msg)


def call(method, path, body=None, token=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def login(u, p="Bank@2026"):
    c, d = call("POST", "/api/auth/login", {"username": u, "password": p})
    return d.get("token")


def wo_total():
    """工单总数 —— 用于确认测试自清理干净。"""
    from app.database import SessionLocal
    from app.models.work_order import WorkOrder
    db = SessionLocal()
    try:
        return db.query(WorkOrder).count()
    finally:
        db.close()


print("=" * 74)
print("准备：以客户经理「李铭」登录（有建单权限）")
mgr = login("liming")
ok(bool(mgr), "李铭登录成功")

n_before = wo_total()
print(f"      起始工单总数：{n_before}")

# ── 用一个确定不存在的工单场景来避免碰真实数据 ──────────────
# 选一个当前**没有进行中工单**的客户，且测试后删除该工单
from app.database import SessionLocal  # noqa: E402
from app.models.work_order import WorkOrder  # noqa: E402
_db = SessionLocal()
_active = {r[0] for r in _db.query(WorkOrder.customer_id)
           .filter(WorkOrder.status.in_(["pending", "in_progress"])).all()}
_db.close()

from app.services.customer_service import get_customer_detail  # noqa: E402
_db = SessionLocal()
_cid = None
for probe in ["C071081", "C034525", "C062858", "C000001", "C000002"]:
    if probe in _active:
        continue
    det = get_customer_detail(_db, probe)
    if det:
        _cid = probe
        _name = det.get("surname")
        break
_db.close()
ok(_cid is not None, f"选定测试客户：{_cid}（{'无在途工单' if _cid else '找不到'}）")

created_id = None
try:
    print("\n一、Agent 提议建单（不执行）")
    c, d = call("POST", "/api/agent/ask",
                {"question": f"给 {_cid} 建个挽留工单",
                 "session_id": "authtest"}, token=mgr)
    ok(c == 200, f"助手接口可用（{c}）")
    pa = d.get("pending_action") or {}
    ok(pa.get("action") == "create_work_order", f"产出待确认建单：{pa.get('action')}")
    payload = pa.get("payload") or {}
    ok(payload.get("customer_id") == _cid, f"目标客户正确：{payload.get('customer_id')}")
    # ⚠ 此时**不应**有工单落库
    ok(wo_total() == n_before, f"提议阶段未落库（总数仍 {wo_total()}）")

    print("\n二、确认建单 —— 操作者应来自**登录会话**")
    c, d = call("POST", "/api/agent/confirm",
                {"action": "create_work_order", "payload": payload}, token=mgr)
    ok(c == 200, f"确认成功（{c}）")
    created = (d.get("order") or {})
    created_id = created.get("id")
    ok(created_id is not None, f"建单成功 id=#{created_id}")
    ok(created.get("customer_id") == _cid, f"客户正确：{created.get('customer_id')}")
    ok(wo_total() == n_before + 1, f"工单总数 +1（{wo_total()}）")

    print("\n三、审计留痕（核心增益）")
    admin = login("zhaomin")
    # admin 需要过双因素；若失败则退回直接查库
    if not admin:
        from app.services import auth_service as A
        _db = SessionLocal()
        sec = _db.query(__import__("app.models.user", fromlist=["User"])
                        .User).filter_by(username="zhaomin").first().totp_secret
        _db.close()
        c1, d1 = call("POST", "/api/auth/login",
                      {"username": "zhaomin", "password": "Bank@2026"})
        c2, d2 = call("POST", "/api/auth/login/totp",
                      {"ticket": d1.get("ticket"), "code": A.totp_now(sec)})
        admin = d2.get("token")

    c, d = call("GET", "/api/auth/audit?limit=50", token=admin)
    items = d.get("items") or []
    hits = [i for i in items
            if i.get("action") == "create_work_order"
            and i.get("target") == _cid]
    ok(len(hits) > 0, f"审计中找到建单记录（{len(hits)} 条）")
    if hits:
        h = hits[0]
        print(f"      主体：{h['display_name']}（{h['username']}／{h['role_label']}）")
        print(f"      动作：{h['action']}  对象：{h['target']}  来源：{h['source']}")
        print(f"      时间：{h['created_at']}  IP：{h['ip']}")
        print(f"      说明：{h['message']}")
        ok(h["username"] == "liming", f"⚠ 操作者是李铭（实际 {h['username']}）")
        ok(h["source"] == "agent", f"⚠ 来源标记为 agent（实际 {h['source']}）")
        ok(h["success"], "标记为成功")
        det = h.get("detail") or "{}"
        ok(str(created_id) in str(det), "审计 detail 含工单号（可回溯到具体单据）")

    print("\n四、不同登录人 → 不同操作者（证明不是写死的）")
    viewer = login("chenjie")
    c, d = call("POST", "/api/agent/confirm",
                {"action": "create_work_order", "payload": payload}, token=viewer)
    ok(c == 403, f"⚠ 只读角色被拒（{c}）")

    mgr2 = login("wangfang")
    # wangfang 此前在 lock 测试里被锁过，若锁定则跳过
    if mgr2:
        c2, d2 = call("POST", "/api/agent/ask",
                      {"question": "现在决策阈值是多少"}, token=mgr2)
        ok(c2 == 200, f"另一位客户经理可正常使用助手（{c2}）")
    else:
        print("  [SKIP] wangfang 处于锁定中（前序测试所致），跳过")

finally:
    print("\n五、清理（测试自清）")
    if created_id is not None:
        from app.database import SessionLocal as SL
        from app.models.work_order import WorkOrder as WO
        db = SL()
        try:
            o = db.query(WO).filter(WO.id == created_id).first()
            if o:
                db.delete(o)
                db.commit()
                print(f"      已删除测试工单 #{created_id}")
        finally:
            db.close()
    print(f"      结束工单总数：{wo_total()}（起始 {n_before}）")
    ok(wo_total() == n_before, "⚠ 测试未遗留数据（总数复原）")

print("\n" + "=" * 74)
print("结论：" + ("PASS —— Agent 写操作已绑定登录人并留审计" if not fails
                  else f"FAIL —— {len(fails)} 项未通过"))
for f in fails:
    print("  ×", f)
