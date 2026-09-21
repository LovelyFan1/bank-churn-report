"""全站接口回归（第二版）—— 用 OpenAPI 实际路由表，不再猜路径。

第一版 7 个 404 全部是探测脚本写错路径（如 /eda/overview、
/customers/{id}/risk-info 在真实路由里根本不存在），不是接口故障。
本版从 /openapi.json 取路由，逐条请求，避免同类误报。
"""
import json
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
EXPECT_N = 96418


def call(url, timeout=240, method="GET", payload=None):
    t0 = time.time()
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.time() - t0


spec = json.loads(call("/openapi.json")[1])
paths = sorted(spec["paths"].keys())

# 只测 GET；POST 训练/聚类类接口会改状态，单独处理
# 路径参数用真实存在的值填充
FILL = {
    "{customer_id}": "C000001",
    "{order_id}": "1",
    "{task_id}": "00000000-0000-0000-0000-000000000000",
    "{feature}": "age",
    "{field}": "age",
}

print("=" * 88)
print(f"{'路径':<48} {'方法':<6} {'码':>4} {'耗时':>8}  说明")
print("=" * 88)

results = []
for p in paths:
    methods = spec["paths"][p]
    if "get" not in methods:
        print(f"{p:<48} {'POST':<6} {'-':>4} {'-':>8}  (写接口，跳过)")
        continue
    url = p
    for k, v in FILL.items():
        url = url.replace(k, v)
    code, body, dt = call(url)
    note = ""
    ok = code == 200
    if ok:
        try:
            d = json.loads(body)
            if isinstance(d, dict) and d.get("error"):
                note = "body.error=" + str(d["error"])[:40]
                ok = False
        except Exception:
            pass
    else:
        note = body[:70].replace("\n", " ")
    results.append((p, url, code, dt, ok, note))
    print(f"{p:<48} {'GET':<6} {str(code):>4} {dt:7.2f}s  {note}")

npass = sum(1 for r in results if r[4])
print()
print("=" * 88)
print(f"GET 接口: {npass}/{len(results)} 通过")

bad = [r for r in results if not r[4]]
if bad:
    print("\n未通过:")
    for p, url, code, dt, ok, note in bad:
        print(f"  [{code}] {url}\n        {note}")

slow = sorted([r for r in results if r[3] > 3.0], key=lambda x: -x[3])
if slow:
    print("\n慢接口 (>3s):")
    for p, url, code, dt, ok, note in slow:
        print(f"  {dt:6.2f}s  {url}")
