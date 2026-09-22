"""性能对照：Agent 端点 vs 普通查询端点。

用途：Agent 一轮 6~8 秒，其中多少是 LLM、多少是自身接口？
本脚本测普通端点的延迟，作为基线 —— 若某个查询接口本身就慢，
Agent 的"慢"就不该全算在 LLM 头上。

⚠ 只读，不写库。
"""
import json
import statistics
import time
import urllib.request

BASE = "http://localhost:8000"

EPS = [
    ("/api/model/risk-info", "模型口径"),
    ("/api/cost-benefit/summary", "成本收益"),
    ("/api/work-orders/stats", "工单统计"),
    ("/api/work-orders?status=pending&page=1&page_size=20", "待处理工单"),
    ("/api/customers/C034525", "单客户详情"),
    ("/api/customers/C034525/suggested-note", "建议理由"),
    ("/api/customers?page=1&page_size=10&sort_by=expected_value", "客户名单 10 条"),
    ("/api/customers?page=1&page_size=100", "客户名单 100 条"),
    ("/api/customers/suggested-notes/batch?customer_ids=C034525&customer_ids=C062858&customer_ids=C071081",
     "批量理由 3 个"),
    ("/api/portfolio/matrix", "策略矩阵"),
    ("/api/eda/key-insights", "EDA 关键发现"),
    ("/api/dashboard/summary", "首屏聚合"),
]

ROUNDS = 3   # 每端点测 3 次取中位，避免单次抖动误判

out = []
out.append("=" * 84)
out.append("普通端点延迟（每端点 3 次取中位）")
out.append("=" * 84)
out.append(f"{'端点':<52}{'中位':>8}{'最小':>8}{'最大':>8}")
out.append("-" * 84)

rows = []
for p, name in EPS:
    times = []
    for _ in range(ROUNDS):
        t0 = time.time()
        try:
            with urllib.request.urlopen(BASE + p, timeout=120) as r:
                r.read()
            times.append(time.time() - t0)
        except Exception as e:
            times.append(float("nan"))
    good = [t for t in times if t == t]
    if not good:
        out.append(f"{name:<52}{'FAIL':>8}")
        continue
    med = statistics.median(good)
    rows.append((name, med, p, len(p)))
    short = (p[:48] + "…") if len(p) > 49 else p
    out.append(f"{name:<52}{med:>7.3f}s{min(good):>7.3f}s{max(good):>7.3f}s")

out.append("")
out.append("=" * 84)
out.append("排序（慢→快）")
out.append("=" * 84)
for name, med, p, L in sorted(rows, key=lambda x: -x[1]):
    out.append(f"  {med:7.3f}s  {name}")

out.append("")
out.append("=" * 84)
out.append("Agent 一轮会调用的端点合计（最坏情况）")
out.append("=" * 84)
by_name = {n: m for n, m, _, _ in rows}
worst = [
    ("list_work_orders", by_name.get("待处理工单", 0)),
    ("list_customers", by_name.get("客户名单 10 条", 0)),
    ("get_customer_risk", by_name.get("单客户详情", 0)
     + by_name.get("建议理由", 0)),
    ("get_model_thresholds", by_name.get("模型口径", 0)),
    ("get_business_summary", by_name.get("成本收益", 0)),
    ("get_workorder_stats", by_name.get("工单统计", 0)),
]
total = sum(v for _, v in worst)
for n, v in worst:
    out.append(f"  {v:7.3f}s  {n}")
out.append(f"  {'-'*40}")
out.append(f"  {total:7.3f}s  合计（若一次全调）")

with open("/tmp/perf_base.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("written")
