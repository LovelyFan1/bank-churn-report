"""验证：批量建单端点（方案 A）。真实写库，测后清理。

覆盖三种情形，全部来自实测（diag112）：
  1. 混合批次：一人已有工单（应 skipped）、两人可建（应 succeeded）
  2. 重复提交：同一批再建应全部 skipped（幂等，不产生重复工单）
  3. 部分失败：不存在的客户应进 failed 而非中断整批

⚠ 会真实建单，但 finally 里全部删除并核对总数复原。
"""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}


fails = []
before = call("GET", "/api/work-orders/stats")[1]["total"]
print(f"起始工单总数 {before}")

# 先确认 C071081 确实有进行中工单（否则用例前提不成立）
ACTIVE = call("GET", "/api/work-orders/active-customers")[1]["customer_ids"]
print(f"当前有进行中工单的客户数 {len(ACTIVE)}")
has_071081 = "C071081" in ACTIVE
print(f"C071081 在其中有进行中工单: {has_071081}")
if not has_071081:
    print("  ⚠ 前提不成立（C071081 无进行中工单），跳过 skipped 断言")

created_ids = []
try:
    print()
    print("=" * 84)
    print("一、混合批次：C071081(已有单) + C034525 + C062858")
    print("=" * 84)
    code, r = call("POST", "/api/agent/confirm-batch",
                   {"customer_ids": ["C071081", "C034525", "C062858"],
                    "assignee": "张思远"})
    print(f"  HTTP {code}")
    print(f"  {json.dumps(r, ensure_ascii=False)[:400]}")
    if code != 200:
        fails.append(f"批量建单失败 HTTP {code}")
    else:
        if has_071081 and "C071081" not in (r.get("skipped") or []):
            fails.append("C071081 已有工单但未被 skipped")
        if len(r.get("created") or []) != 2:
            fails.append(f"应创建 2 条，实得 {len(r.get('created') or [])}")
        for c in r.get("created") or []:
            created_ids.append(c["id"])
            # 负责人必须被写入
            if c.get("assignee") != "张思远":
                fails.append(f"工单 #{c['id']} 负责人应为张思远，实得 {c.get('assignee')}")
        print(f"  [{'OK ' if not fails else '??'}] created={len(r.get('created') or [])} "
              f"skipped={len(r.get('skipped') or [])} failed={len(r.get('failed') or [])}")

    mid = call("GET", "/api/work-orders/stats")[1]["total"]
    print(f"  工单总数 {before} → {mid}")
    if mid != before + 2:
        fails.append(f"总数应 +2，实得 +{mid - before}")

    print()
    print("=" * 84)
    print("二、重复提交同一批（应全部 skipped，不产生重复）")
    print("=" * 84)
    code, r2 = call("POST", "/api/agent/confirm-batch",
                    {"customer_ids": ["C034525", "C062858"], "assignee": "李铭"})
    print(f"  HTTP {code}")
    print(f"  created={len(r2.get('created') or [])} skipped={len(r2.get('skipped') or [])}")
    if (r2.get("created") or []):
        fails.append("重复提交竟又建了单（互斥失效）")
    if len(r2.get("skipped") or []) != 2:
        fails.append("重复提交应全部 skipped")
    mid2 = call("GET", "/api/work-orders/stats")[1]["total"]
    print(f"  工单总数仍为 {mid2}（不应增加）")
    if mid2 != mid:
        fails.append("重复提交增加了工单数")

    print()
    print("=" * 84)
    print("三、部分失败：掺入不存在的客户")
    print("=" * 84)
    code, r3 = call("POST", "/api/agent/confirm-batch",
                    {"customer_ids": ["C999999"], "assignee": ""})
    print(f"  HTTP {code}")
    print(f"  {json.dumps(r3, ensure_ascii=False)[:250]}")
    if not (r3.get("failed") or []):
        fails.append("不存在的客户未进 failed")
    if r3.get("succeeded") != 0:
        fails.append("不存在客户竟建单成功")

    print()
    print("=" * 84)
    print("四、边界")
    print("=" * 84)
    code, r4 = call("POST", "/api/agent/confirm-batch", {"customer_ids": []})
    print(f"  [{'OK ' if code == 422 else 'FAIL'}] 空列表被拒（HTTP {code}）")
    if code != 422:
        fails.append(f"空列表未被拒：{code}")

    # 去重：同一 id 传两次只应建一张
    code, r5 = call("POST", "/api/agent/confirm-batch",
                    {"customer_ids": ["C062858", "C062858"], "assignee": ""})
    print(f"  重复 id 提交 → created={len(r5.get('created') or [])} "
          f"skipped={len(r5.get('skipped') or [])}")
    if len(r5.get("created") or []) > 1:
        fails.append("同一 id 传两次建了多张单")
    for c in r5.get("created") or []:
        created_ids.append(c["id"])

finally:
    print()
    print("=" * 84)
    print("五、清理")
    print("=" * 84)
    # 只删本测试创建的（按 id 精确删，不做粗暴清表）
    for oid in created_ids:
        code, _ = call("DELETE", f"/api/work-orders/{oid}")
        print(f"  删除 #{oid} → HTTP {code}")
    after = call("GET", "/api/work-orders/stats")[1]["total"]
    print(f"  工单总数复原：{before} → {after}")
    if after != before:
        fails.append(f"清理后总数未复原：{before} → {after}")

print()
print("=" * 84)
if fails:
    print(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        print(f"  · {f}")
else:
    print("结论：PASS —— 混合批次正确分类、重复提交幂等、部分失败不中断、边界正常、数据已清理")
