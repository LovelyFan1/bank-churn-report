"""核验：缩量后全系统「客户数」口径是否一致，且无残留 100000。

缩量改造最容易留下的隐患是：某个接口/前端硬编码了 100000，
或某处缓存未失效仍在用旧数。本脚本遍历所有接口，
把落在大数区间的数值全部捞出来比对。
"""
import json
import urllib.request

BASE = "http://localhost:8000"
EXPECT = 96418

GETS = [
    "/api/dashboard/summary",
    "/api/customers?page=1&page_size=1",
    "/api/customers/export",
    "/api/cluster/profiles",
    "/api/cost-benefit/summary",
    "/api/cost-benefit/thresholds",
    "/api/cost-benefit/retention-summary",
    "/api/model/risk-info",
    "/api/model/comparison",
    "/api/model/batch-score?top_n=100",
    "/api/eda/key-insights",
    "/api/eda/comprehensive",
    "/api/eda/churn-analysis",
    "/api/eda/product-overload",
    "/api/data/overview",
    "/api/portfolio/matrix",
    "/api/work-orders/stats",
]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=240) as r:
        return json.loads(r.read().decode("utf-8"))


# 收集所有"像客户总数"的值
hits = {}
for ep in GETS:
    try:
        d = get(ep)
    except Exception as e:
        print(f"[跳过] {ep}: {type(e).__name__}")
        continue

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}" if path else k
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)) and 50000 <= v <= 200000:
                    hits.setdefault(p, set()).add(v)
                elif isinstance(v, str) and v.isdigit() and 50000 <= int(v) <= 200000:
                    hits.setdefault(p, set()).add(int(v))
                else:
                    walk(v, p)
        elif isinstance(node, list):
            for x in node[:3]:
                walk(x, path + "[]")

    walk(d)

print("=" * 78)
print("所有落在 [50000, 200000] 区间的数值字段")
print("=" * 78)
leaked = []
for p in sorted(hits):
    vals = sorted(hits[p])
    marks = []
    for v in vals:
        if v == EXPECT:
            marks.append(f"{v:,} <- 正确")
        elif v == 100000:
            marks.append(f"{v:,} <- 旧值残留!")
            leaked.append((p, v))
        else:
            marks.append(f"{v:,}")
    print(f"  {p:<48} {' | '.join(marks)}")

print()
print("=" * 78)
print("判定")
print("=" * 78)
if leaked:
    print(f"发现 {len(leaked)} 处仍为旧值 100000:")
    for p, v in leaked:
        print(f"  {p} = {v:,}")
else:
    print(f"未发现 100000 残留。期望值 {EXPECT:,} 出现于 {sum(1 for p in hits if EXPECT in hits[p])} 个字段。")

# 明确核对几个关键口径
print()
print("关键口径逐项核对:")
checks = {
    "/api/dashboard/summary": ["total_customers"],
    "/api/cost-benefit/summary": ["total_customers", "total_scored", "total"],
    "/api/model/risk-info": ["total_customers", "total_scored"],
    "/api/eda/comprehensive": ["total_customers"],
}
for ep, keys in checks.items():
    try:
        d = get(ep)
    except Exception as e:
        print(f"  {ep}: 请求失败 {e}")
        continue
    for k in keys:
        if k in d:
            v = d[k]
            mark = "OK" if v == EXPECT else f"DIFF (期望 {EXPECT:,})"
            print(f"  {ep:<34} {k:<18} = {v:>8,}  {mark}")
