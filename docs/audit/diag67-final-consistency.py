"""最终一致性总检 —— 把这次缩量的所有断言集中复验一遍。

每条断言都给出「期望值 / 实测值 / 判定」，便于一眼看清是否有遗漏。
"""
import json
import sqlite3
import urllib.request

import pandas as pd

CSV = "/tmp/new_96418.csv"
ORIG = "/tmp/orig_100k.csv"
DB = "/app/data/churn_analysis.db"
BASE = "http://localhost:8000"
N = 96418

rows = []


def check(name, expect, actual, ok=None):
    if ok is None:
        ok = expect == actual
    rows.append((ok, name, str(expect), str(actual)))


# ── 1) 文件层 ──
new = pd.read_csv(CSV)
orig = pd.read_csv(ORIG)
check("CSV 行数", N, len(new))
check("CSV 列数", 34, new.shape[1])

cut = orig.head(N).reset_index(drop=True)
cmp_cols = [c for c in orig.columns if c != "row_number"]
all_same = all(
    (cut[c].fillna(-9e18) == new[c].fillna(-9e18)).all()
    if cut[c].dtype.kind in "fi" else
    (cut[c].astype(str) == new[c].astype(str)).all()
    for c in cmp_cols
)
check("新CSV == 原始前96418行（33列逐格）", True, all_same)
check("row_number 重编 1..N", True,
      bool((new["row_number"].values == range(1, N + 1)).all()))
check("最大 customer_id", "C096418", new["customer_id"].max())

# 派生文件
mo = pd.read_csv("/tmp/monthly.csv", usecols=["customer_id"])
tx = pd.read_csv("/tmp/txn.csv", usecols=["customer_id"])
check("monthly 行数", 2314032, len(mo))
check("monthly 唯一客户", N, mo["customer_id"].nunique())
check("monthly 最大编号", "C096418", mo["customer_id"].max())
check("txn 行数", 296981, len(tx))
check("txn 最大编号 <= C096418", True, tx["customer_id"].max() <= "C096418")

# ── 2) 数据库层 ──
c = sqlite3.connect(DB)
check("DB customers 行数", N, c.execute("SELECT COUNT(*) FROM customers").fetchone()[0])
check("DB 最大 customer_id", "C096418",
      c.execute("SELECT MAX(customer_id) FROM customers").fetchone()[0])
check("DB 流失率", 0.2040,
      round(c.execute("SELECT AVG(exited) FROM customers").fetchone()[0], 4))
check("DB 中是否残留 C100000", 0,
      c.execute("SELECT COUNT(*) FROM customers WHERE customer_id='C100000'").fetchone()[0])

# 工单
check("工单数", 60, c.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0])
dangling = c.execute(
    "SELECT COUNT(*) FROM work_orders w LEFT JOIN customers c "
    "ON c.customer_id=w.customer_id WHERE c.customer_id IS NULL").fetchone()[0]
check("工单悬空数", 0, dangling)
name_diff = c.execute(
    "SELECT COUNT(*) FROM work_orders w JOIN customers c "
    "ON c.customer_id=w.customer_id WHERE c.surname <> w.customer_name").fetchone()[0]
check("工单姓名不一致数", 0, name_diff)

# ── 3) 接口层 ──
#
# ⚠ 字段路径按实际响应结构写（此前三处 FAIL 都是我断言写错，非接口问题）：
#   - dashboard 的客户数在 overview / business_summary 两个**嵌套层**里，
#     顶层没有 total_customers 这个键。
#   - /model/risk-info 返回的是阈值与决策指标（thresholds / decision_metrics），
#     **本来就不含 total_customers**；客户总数应查 decision_metrics.sample_size
#     （那是测试集规模）或 dashboard。此处改为断言 key 存在性 + 阈值合理性。
#   - 上一版把 total 与 total_pages 归并到同名 key，导致 total_pages
#     被 total(96418) 覆盖而误报；本版分开断言。
def get(p):
    with urllib.request.urlopen(BASE + p, timeout=240) as r:
        return json.loads(r.read().decode())

d = get("/api/dashboard/summary")
check("dashboard overview.total_customers", N, d["overview"]["total_customers"])
check("dashboard business_summary.total_customers", N,
      d["business_summary"]["total_customers"])

d = get("/api/cost-benefit/summary")
check("cost-benefit total_customers", N, d.get("total_customers"))

d = get("/api/model/risk-info")
check("risk-info 含 thresholds", True, "thresholds" in d)
check("risk-info 含 decision_metrics", True, "decision_metrics" in d)
check("risk-info 最优阈值 == 0.2", 0.2, d.get("optimal_threshold"))

d = get("/api/customers?page=1&page_size=1")
check("customers total", N, d.get("total"))
check("customers total_pages(page_size=1)", N, d.get("total_pages"))

d20 = get("/api/customers?page=1&page_size=20")
check("customers total_pages(page_size=20)", 4821, d20.get("total_pages"))
check("customers total(page_size=20)", N, d20.get("total"))

d = get("/api/cluster/profiles")
sizes = {x["cluster_id"]: x["count"] for x in d["clusters"]}
check("簇数", 5, len(sizes))
check("簇人数合计", N, sum(sizes.values()))
names = [x["name"] for x in d["clusters"]]
check("簇名互不相同", 5, len(set(names)))

d = get("/api/cluster/3d-scatter")
check("散点降维方法", "tsne", d.get("method"))
check("散点 total_points", N, d.get("total_points"))

d = get("/api/model/comparison")
best = max(d["models"], key=lambda m: m["auc"]) if isinstance(d.get("models"), list) else None
check("模型数", 5, len(d["models"]) if isinstance(d.get("models"), list) else 0)

# ── 输出 ──
print("=" * 82)
print(f"{'':2} {'断言':<42} {'期望':>16} {'实测':>16}")
print("=" * 82)
npass = 0
for ok, name, exp, act in rows:
    if ok:
        npass += 1
    print(f"{'OK' if ok else 'FAIL':<4} {name:<42} {exp:>16} {act:>16}")
print("=" * 82)
print(f"总计: {npass}/{len(rows)} 通过")
if npass < len(rows):
    print("\n未通过:")
    for ok, name, exp, act in rows:
        if not ok:
            print(f"  {name}: 期望 {exp}, 实测 {act}")
