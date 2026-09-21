"""核验：缩量后全系统的「客户数口径」是否处处一致。

背景：这次把数据从 100000 改成 96418，需要确认所有对外暴露客户数的
接口都给出**同一个数**，且没有任何地方硬编码 100000 或残留旧值。
"""
import json
import urllib.request

BASE = "http://localhost:8000/api"

ENDPOINTS = [
    "/dashboard/summary",
    "/customers?page=1&page_size=1",
    "/cluster/profiles",
    "/cost-benefit/summary",
    "/model/risk-info",
    "/eda/key-insights",
]


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


print("=" * 70)
print("各接口对外暴露的客户数口径")
print("=" * 70)

found = {}
for ep in ENDPOINTS:
    try:
        d = get(ep)
    except Exception as e:
        print(f"\n{ep}\n  请求失败: {type(e).__name__}: {e}")
        continue

    print(f"\n{ep}")
    # 递归找所有可能是"客户总数"的字段
    hits = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{path}.{k}" if path else k
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    lk = k.lower()
                    if ("total" in lk or "count" in lk or "num" in lk
                            or "sample" in lk) and 50000 < v < 200000:
                        hits.append((p, v))
                else:
                    walk(v, p)
        elif isinstance(node, list) and node and isinstance(node[0], dict):
            walk(node[0], path + "[0]")

    walk(d)
    for p, v in hits:
        print(f"    {p:46s} = {v:,}")
        found.setdefault(p, set()).add(v)

# 聚合字段按名字比对（不同接口的同一字段必须同值）
by_name = {}
for p, v in [(p, v) for p, vs in found.items() for v in vs]:
    key = p.split(".")[-1].split("[")[0]
    by_name.setdefault(key, {}).setdefault(p, v)

print()
print("=" * 70)
print("同一语义字段是否跨接口一致")
print("=" * 70)
for key in sorted(by_name):
    vals = set(by_name[key].values())
    mark = "OK  " if len(vals) == 1 else "DIFF"
    print(f"[{mark}] {key:24s} 取值={sorted(vals)}")
    for p, v in by_name[key].items():
        print(f"          {p} = {v:,}")

print()
print("期望客户总数 = 96,418")
