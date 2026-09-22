"""匿名化实测 —— 穷举所有可能泄露客户身份的出口。

核心断言：
  A. viewer（只读分析）拿不到任何**身份字段**（姓名/编号/余额/概率/风险因素）
  B. viewer 仍能拿到**聚合统计**（总数/流失率/风险分布）
  C. manager / admin 完全不受影响（回归）

⚠ 只读：全部为 GET。不建单、不改状态、不删任何东西。
"""

import json
import urllib.error
import urllib.request

B = "http://127.0.0.1:8000"
fails = []

# 客户身份字段 —— 出现在响应里即视为泄露。
#
# ⚠ `note` 与 `channel` **不在**此集合，实测核对过：
#     dashboard 的 `note` 是成本模型的**口径说明文本**（"基于测试集指标、
#     假设客单价…"），与客户无关；工单的 `channel` 是渠道枚举值
#     （relationship / outbound / automated），本身不指向具体的人。
#   把它们当身份字段会产出**假告警** —— 实测踩过：3 项 FAIL 全是误报。
#   判据应是"这个字段能否定位到自然人"，不是"字段名听起来像不像客户信息"。
IDENTITY_KEYS = {
    "surname", "customer_id", "customer_name", "balance", "probability",
    "risk_factors", "expected_value", "age", "gender", "credit_score",
    "estimated_salary", "geography", "strategy", "reason",
    "action", "num_products", "is_active_member",
}


def call(p, tok, m="GET"):
    r = urllib.request.Request(B + p, method=m)
    r.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(r, timeout=120) as x:
            raw = x.read().decode()
            return x.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def login(u, pwd="Bank@2026"):
    r = urllib.request.Request(
        B + "/api/auth/login",
        data=json.dumps({"username": u, "password": pwd}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode())


def login_admin():
    d = login("zhaomin")
    if not d.get("need_totp"):
        return d["token"]
    from app.services import auth_service as A
    import sqlite3
    con = sqlite3.connect("file:/app/data/churn_analysis.db?mode=ro", uri=True)
    sec = con.execute(
        "SELECT totp_secret FROM users WHERE username='zhaomin'").fetchone()[0]
    con.close()
    r = urllib.request.Request(
        B + "/api/auth/login/totp",
        data=json.dumps({"ticket": d["ticket"], "code": A.totp_now(sec)}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=30).read().decode())["token"]


def ok(cond, msg):
    print(f"  [{'OK ' if cond else 'FAIL'}] {msg}")
    if not cond:
        fails.append(msg)


def leaked_keys(obj, limit=200):
    """递归找出响应里出现的身份字段名。"""
    found = set()

    def walk(o, depth=0):
        if depth > 8 or len(found) > limit:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if k in IDENTITY_KEYS:
                    found.add(k)
                walk(v, depth + 1)
        elif isinstance(o, list):
            for it in o[:5]:
                walk(it, depth + 1)

    walk(obj)
    return found


viewer = login("chenjie")["token"]
mgr = login("liming")["token"]
admin = login_admin()

print("=" * 76)
print("一、viewer 的每个客户数据出口：不得出现身份字段")
endpoints = [
    ("/api/customers?page_size=5", "客户列表"),
    ("/api/customers?page_size=5&risk_level=CRITICAL", "客户列表(筛选)"),
    ("/api/dashboard/summary", "工作台首屏(含Top10)"),
    ("/api/work-orders?page_size=5", "工单列表"),
    ("/api/work-orders/1", "工单详情"),
    ("/api/work-orders/active-customers", "活跃客户编号"),
    ("/api/customers/C071081", "客户详情"),
    ("/api/customers/C071081/suggested-note", "建单建议理由"),
    ("/api/customers/suggested-notes/batch?customer_ids=C071081", "批量建议理由"),
    ("/api/customers/export", "CSV 导出"),
]
for p, desc in endpoints:
    c, d = call(p, viewer)
    lk = leaked_keys(d)
    # 403 也算通过（明确拒绝）
    if c == 403:
        print(f"  [OK ] {desc:22s} {p.split('?')[0]:44s} → 403 明确拒绝")
        continue
    if c == 200 and p.endswith("/export"):
        # CSV 未鉴权成功即为泄露
        print(f"  [FAIL] {desc:22s} → 200（导出未被拦）")
        fails.append(f"{desc} 未被拦")
        continue
    ok(not lk, f"{desc:22s} {p.split('?')[0]:44s} → {c}  泄露字段={sorted(lk) or '无'}")

print("\n二、viewer 仍应拿到聚合统计（权限收窄不能把他变成瞎子）")
c, d = call("/api/dashboard/summary", viewer)
ov = d.get("overview") or {}
print(f"     总客户={ov.get('total_customers')} 流失率={ov.get('churn_rate')}% "
      f"风险分布={d.get('risk_distribution')}")
ok(ov.get("total_customers") == 96418, "客户总数可见")
ok(bool(d.get("risk_distribution")), "风险分布可见")
ok((d.get("business_summary") or {}) != {}, "成本收益可见")

c, d = call("/api/customers?page_size=5", viewer)
it = (d.get("items") or [])
print(f"     列表条目示例: {json.dumps(it[0], ensure_ascii=False) if it else '(空)'}")
ok(len(it) > 0, "匿名列表非空（能看到批次规模）")
ok(d.get("masked") is True, "响应明确标注 masked=true")
ok(bool(d.get("mask_notice")), "附带权限说明文案")
ok(all("seq" in x for x in it), "每条都有 seq 序号")

print("\n三、viewer 列表的**可用信息**应保留风险等级与价值层")
if it:
    ok(all("risk_level" in x for x in it), "风险等级保留")
    ok(all("value_tier" in x for x in it), "价值层保留")

print("\n四、聚合分析页不受影响（本就无身份信息）")
for p, desc in [("/api/eda/key-insights", "EDA 洞察"),
                ("/api/model/comparison", "模型对比"),
                ("/api/portfolio/matrix", "价值层矩阵"),
                ("/api/cluster/profiles", "聚类画像"),
                ("/api/cost-benefit/retention-summary", "挽留效果")]:
    c, d = call(p, viewer)
    ok(c == 200, f"{desc:14s} {p:38s} → {c}")

print("\n五、manager / admin 完全不受影响（回归）")
for tok, who in [(mgr, "manager"), (admin, "admin")]:
    c, d = call("/api/customers?page_size=3", tok)
    lk = leaked_keys(d)
    it2 = d.get("items") or []
    name = it2[0].get("surname") if it2 else None
    ok(c == 200 and "surname" in (it2[0] if it2 else {}),
       f"{who}: 客户列表可看到姓名（示例 {name}）")
    ok(d.get("masked") is False, f"{who}: masked=false")
    c, d = call("/api/customers/C071081", tok)
    ok(c == 200, f"{who}: 客户详情可访问")
    c, d = call("/api/work-orders?page_size=3", tok)
    it3 = d.get("items") or []
    ok(c == 200 and (not it3 or "customer_name" in it3[0]),
       f"{who}: 工单列表可看到客户名")
    c, d = call("/api/dashboard/summary", tok)
    tc = (d.get("top_customers") or [])
    ok(bool(tc) and "surname" in tc[0], f"{who}: 工作台 Top10 可看到姓名")

print("\n" + "=" * 76)
print("结论：" + ("PASS —— 只读角色看不到任何身份信息，且聚合统计不受影响"
                  if not fails else f"FAIL —— {len(fails)} 项"))
for f in fails:
    print("  ×", f)
