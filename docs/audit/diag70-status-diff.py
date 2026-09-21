"""比对：跑探针前的备份 vs 当前库，找出工单状态的实际变化。

背景疑点：
  - 早先直接查库，id 61/62 是 pending
  - 现在通过 API 查，一条 pending 都没有（in_progress 8 / completed 45 / lost 9）
  若非我的探针所致（探针只建删了 id 63），则说明**状态确实被改过** ——
  这直接关系到用户「无法调整状态」的说法是否成立。

同时核对：PUT 改状态是否真的落库、有没有别的地方偷偷改。
"""
import sqlite3

CUR = "/app/data/churn_analysis.db"
BAK = "/app/data/churn_analysis.db.before-statemachine"

cur = sqlite3.connect(CUR)
bak = sqlite3.connect(BAK)


def snap(conn):
    rows = conn.execute(
        "SELECT id, customer_id, status, result, assignee, "
        "completed_at, updated_at FROM work_orders ORDER BY id").fetchall()
    return {r[0]: r for r in rows}


a = snap(bak)
b = snap(cur)

print(f"备份工单数 = {len(a)}   当前工单数 = {len(b)}")
print()

print("=== 状态分布 ===")
for label, s in [("备份", a), ("当前", b)]:
    dist = {}
    for r in s.values():
        dist[r[2]] = dist.get(r[2], 0) + 1
    print(f"  {label}: {dict(sorted(dist.items()))}")

print()
print("=== 逐条差异 ===")
diff = 0
for oid in sorted(set(a) | set(b)):
    ra, rb = a.get(oid), b.get(oid)
    if ra is None:
        print(f"  #{oid} 备份无 → 当前有: {rb}")
        diff += 1
    elif rb is None:
        print(f"  #{oid} 备份有 → 当前无: {ra}")
        diff += 1
    elif ra[2] != rb[2] or ra[3] != rb[3]:
        print(f"  #{oid} ({ra[1]})")
        print(f"       备份: status={ra[2]:<12} result={ra[3]}")
        print(f"       当前: status={rb[2]:<12} result={rb[3]}")
        print(f"       completed_at 备份={ra[5]}  当前={rb[5]}")
        diff += 1

print(f"\n差异条数 = {diff}")
if diff == 0:
    print("→ 无差异：说明 61/62 的 pending 在我跑探针**之前**就已不是 pending？")
    print("  （需回看更早的备份确认）")
