"""全站接口回归 —— 缩量到 96418 后逐接口检查。

关注两类问题：
  1) 是否有接口报错（500 / 结构缺失）
  2) 是否有接口仍暴露旧的 100000，或客户数口径互相矛盾
"""
import json
import time
import urllib.request
import urllib.error

BASE = "http://localhost:8000/api"
EXPECT_N = 96418

GETS = [
    "/dashboard/summary",
    "/customers?page=1&page_size=20&sort_by=expected_value&sort_order=desc",
    "/customers/C000001",
    "/customers/C000001/risk-info",
    "/customers/C000001/work-orders",
    "/work-orders?page=1&page_size=20",
    "/work-orders/stats",
    "/work-orders/active-customers",
    "/intervention/strategies",
    "/cluster/profiles",
    "/cluster/3d-scatter",
    "/eda/overview",
    "/eda/distributions",
    "/eda/correlation",
    "/eda/key-insights",
    "/eda/distribution/age",
    "/eda/distribution/balance",
    "/model/comparison",
    "/model/risk-info",
    "/model/roc-curves",
    "/model/feature-importance",
    "/model/confusion-matrices",
    "/model/shap-global",
    "/model/batch-score?top_n=100",
    "/cost-benefit/summary",
    "/cost-benefit/matrix",
    "/portfolio/matrix",
    "/portfolio/summary",
]


def get(path, timeout=240):
    t0 = time.time()
    req = urllib.request.Request(BASE + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8")
            code = r.status
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.time() - t0
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.time() - t0
    return code, body, time.time() - t0


print("=" * 84)
print(f"{'接口':<52} {'码':>4} {'耗时':>8}  说明")
print("=" * 84)

fails = []
slow = []
for ep in GETS:
    code, body, dt = get(ep)
    note = ""
    if code != 200:
        note = "FAIL " + body[:60].replace("\n", " ")
        fails.append((ep, code, body[:200]))
    else:
        try:
            d = json.loads(body)
            if isinstance(d, dict) and d.get("error"):
                note = "body.error = " + str(d["error"])[:50]
                fails.append((ep, 200, str(d["error"])[:200]))
        except Exception:
            pass
    if dt > 3.0:
        slow.append((ep, dt))
    print(f"{ep:<52} {str(code):>4} {dt:7.2f}s  {note}")

print()
print("=" * 84)
print(f"结果: {len(GETS) - len(fails)}/{len(GETS)} 通过")
if fails:
    print("\n失败项:")
    for ep, code, b in fails:
        print(f"  [{code}] {ep}\n        {b}")
if slow:
    print("\n慢接口 (>3s):")
    for ep, dt in slow:
        print(f"  {dt:6.2f}s  {ep}")
