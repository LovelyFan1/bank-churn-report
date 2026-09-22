"""验证：确认执行的写路径能真正建单，且字段完整（测后清理）。

为什么必须真写一次（而不是只拦截）：
  /api/agent/confirm 把 propose 工具产出的 payload 直接喂给
  WorkOrderCreate(**body)。payload 的字段名是**工具层自己拼的**，
  与前端表单传的不完全同源 —— 若字段名写错，Pydantic 会用默认值
  静默接受（如 risk_level 默认 MEDIUM），造出一张**字段错误的工单**。
  这类错误只有真写一次并核对入库字段才能发现。

⚠ 本脚本会真实建单，但**测后立即删除**，并在结尾核对工单总数复原。
"""
import json
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}


fails = []
before = call("GET", "/api/work-orders/stats")[1]["total"]
print(f"起始工单总数: {before}")

print()
print("=" * 88)
print("一、让 Agent 生成待确认动作")
print("=" * 88)
code, r = call("POST", "/api/agent/ask",
               {"question": "帮我给 C034525 建个挽留工单，负责人写张思远"})
pa = r.get("pending_action")
print(f"  HTTP {code}  basis={r.get('basis')}  pending={'有' if pa else '无'}")
if not pa:
    fails.append("未生成待确认动作")
    print(f"\n结论：FAIL —— {fails}")
    raise SystemExit(1)
print(f"  summary: {pa['summary']}")

created_id = None
try:
    print()
    print("=" * 88)
    print("二、确认执行（真实写库）")
    print("=" * 88)
    code, res = call("POST", "/api/agent/confirm",
                     {"action": pa["action"], "payload": pa["payload"]})
    print(f"  HTTP {code}")
    if code != 200:
        fails.append(f"确认建单失败：{res}")
        print(f"  {json.dumps(res, ensure_ascii=False)[:300]}")
    else:
        order = res.get("order") or {}
        created_id = order.get("id")
        print(f"  已创建工单 #{created_id}")
        print()
        print("  入库字段核对（关键：payload 字段名是否被正确接收）:")
        expects = {
            "customer_id": "C034525",
            "customer_name": "Goddard",
            "risk_level": "CRITICAL",
            "assignee": "张思远",
            "value_tier_snapshot": "HIGH",
            "channel": "relationship",
            "status": "pending",
        }
        for k, want in expects.items():
            got = order.get(k)
            ok = got == want
            if not ok:
                fails.append(f"字段 {k} 期望 {want!r} 实得 {got!r}")
            print(f"    [{'OK ' if ok else 'FAIL'}] {k:<24} = {got!r}")

        # note 必须非空（系统自动生成的理由）
        note = order.get("note") or ""
        if len(note) < 20:
            fails.append("note 未写入")
        print(f"    [{'OK ' if len(note) >= 20 else 'FAIL'}] note 长度 {len(note)}")

        # 快照必须被写入（否则事后无法审计）
        for k in ["thresholds_snapshot", "model_used", "expected_value_snapshot"]:
            v = order.get(k)
            ok = v not in (None, "", 0)
            if not ok:
                fails.append(f"快照 {k} 未写入")
            print(f"    [{'OK ' if ok else 'FAIL'}] {k:<24} = {str(v)[:44]}")

    print()
    print("=" * 88)
    print("三、工单总数应 +1")
    print("=" * 88)
    mid = call("GET", "/api/work-orders/stats")[1]["total"]
    ok = mid == before + 1
    if not ok:
        fails.append(f"总数未 +1：{before} → {mid}")
    print(f"  [{'OK ' if ok else 'FAIL'}] {before} → {mid}")

finally:
    # ── 清理：无论成败都删掉测试工单 ──────────────────────
    print()
    print("=" * 88)
    print("四、清理测试数据")
    print("=" * 88)
    if created_id:
        code, _ = call("DELETE", f"/api/work-orders/{created_id}")
        print(f"  删除 #{created_id} → HTTP {code}")
    after = call("GET", "/api/work-orders/stats")[1]["total"]
    ok = after == before
    if not ok:
        fails.append(f"清理后总数未复原：{before} → {after}")
    print(f"  [{'OK ' if ok else 'FAIL'}] 总数复原：{before} → {after}")

print()
print("=" * 88)
if fails:
    print(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        print(f"  · {f}")
else:
    print("结论：PASS —— 确认路径可真实建单、字段完整、快照已写入、测试数据已清理")
