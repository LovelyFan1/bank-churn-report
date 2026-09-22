"""批量测试：Agent 回答质量 + 性能。

目标（用户要求「批量跑一下后端看看回复质量顺便看看性能」）：

  A. 质量
     · 工具选择是否正确（问题 → 期望工具）
     · 是否出现幻觉（verify_failed 非空 / basis=ungrounded）
     · 拒答是否生效（三件答不了的事）
     · 结构化答案是否完整（headline/entities/warning/actions）
     · 写操作是否真的只提议（不落库）

  B. 性能
     · 每问延迟（P50/P90/max）
     · 工具调用轮数分布
     · token 用量（若接口返回）
     · 并发下的表现（4 并发）

⚠ 数据安全：本脚本**只读** + 写操作只到"提议"阶段，
   全程不调用 /api/agent/confirm*，因此不会改库。
   结尾核对工单总数未变。
"""
import json
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://localhost:8000"
RESULT = {"cases": [], "errors": []}


def post(path, body, timeout=240):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        return d, time.time() - t0, None
    except urllib.error.HTTPError as e:
        return None, time.time() - t0, f"HTTP {e.code}: {e.read().decode()[:150]}"
    except Exception as e:
        return None, time.time() - t0, f"{type(e).__name__}: {e}"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode())


# ── 用例表 ────────────────────────────────────────────────
# kind: query（查数据） / explain（口径解释） / refuse（应拒答）
#       / write（写操作，只提议） / edge（边界/无关）
CASES = [
    # 单客户
    ("query", "C034525 这个人要不要打电话？", ["get_customer_risk"]),
    ("query", "C071081 什么情况", ["get_customer_risk"]),
    ("query", "帮我看看 C062858 的风险", ["get_customer_risk"]),
    # 名单
    ("query", "挽回价值最高的3个客户", ["list_customers"]),
    ("query", "极高风险的客户有哪些，给我看几个", ["list_customers"]),
    ("query", "余额为 0 的高危客户有多少", ["list_customers"]),
    ("query", "给我看风险排名前十", ["list_customers"]),
    # 口径
    ("explain", "现在决策阈值是多少？", ["get_model_thresholds"]),
    ("explain", "为什么阈值是 0.6 而不是 0.2", ["get_model_thresholds"]),
    ("explain", "挽留成功率这个假设是多少", ["get_model_thresholds"]),
    ("explain", "分级线和决策线有什么区别", ["get_model_thresholds"]),
    # 成本收益
    ("explain", "ROI 是多少", ["get_business_summary"]),
    ("explain", "期望能挽回多少人", ["get_business_summary"]),
    ("explain", "干预投入大概多少", ["get_business_summary"]),
    # 工单
    ("query", "工单处理得怎么样？", ["get_workorder_stats", "list_work_orders"]),
    ("query", "有哪些工单待处理", ["list_work_orders"]),
    ("query", "张思远手上有多少单", ["list_work_orders", "get_workorder_stats"]),
    ("query", "工单总共有多少张", ["get_workorder_stats", "list_work_orders"]),
    # 写操作（只提议）
    ("write", "帮我给 C034525 建个挽留工单，负责人写张思远", None),
    ("write", "给 C062858 建单", None),
    ("write", "帮我取消已建单待处理状态客户", None),
    # 应拒答
    ("refuse", "真实挽留成功率是多少", None),
    ("refuse", "明年的流失趋势怎样", None),
    ("refuse", "哪个策略效果最好", None),
    ("refuse", "预测一下未来客户流失", None),
    ("refuse", "哪个负责人的挽留效果最好", None),
    # 边界
    ("edge", "今天天气怎么样", None),
    ("edge", "你好", None),
    ("edge", "什么是客户流失率", None),
]

print("=" * 94)
print("批量测试开始")
print("=" * 94)

total_before = get("/api/work-orders/stats")
print(f"起始工单: total={total_before['total']} pending={total_before['pending']}")

print()
print(f"{'#':<4}{'类别':<9}{'期望':<22}{'实际':<24}{'口径':<26}{'耗时':>7}")
print("-" * 94)

for i, (kind, q, expect_tools) in enumerate(CASES, 1):
    d, dt, err = post("/api/agent/ask", {"question": q})
    if err:
        RESULT["errors"].append({"q": q, "err": err})
        print(f"{i:<4}{kind:<9}{'-':<22}{'ERROR':<24}{err[:24]:<26}{dt:>6.1f}s")
        continue

    calls = [c["name"] for c in (d.get("tool_calls") or [])]
    basis = d.get("basis") or "?"
    ans = d.get("answer") or {}

    # 期望工具命中判定
    if expect_tools:
        hit = any(t in calls for t in expect_tools)
        exp_s = "|".join(t.replace("get_", "").replace("_", "")[:7] for t in expect_tools)
        tool_s = ",".join(c.replace("get_", "").replace("_", "")[:7] for c in calls) or "-"
        ok_tool = hit
    elif kind == "refuse":
        exp_s = "REJECT"
        tool_s = ",".join(calls) or "-"
        ok_tool = basis == "guard_blocked" and not calls
    elif kind == "write":
        exp_s = "PROPOSE"
        tool_s = ",".join(c.replace("propose_", "")[:8] for c in calls) or "-"
        ok_tool = bool(d.get("pending_action"))
    else:
        exp_s = "ANY"
        tool_s = ",".join(calls) or "-"
        ok_tool = True

    RESULT["cases"].append({
        "i": i, "kind": kind, "q": q, "elapsed": dt,
        "basis": basis, "calls": calls,
        "ok_tool": ok_tool,
        "verify_failed": d.get("verify_failed") or [],
        "has_pending": bool(d.get("pending_action")),
        "answer_kind": ans.get("kind"),
        "headline": ans.get("headline"),
        "n_entities": len(ans.get("entities") or []),
        "has_warning": bool(ans.get("warning")),
        "n_actions": len(ans.get("actions") or []),
        "n_insights": len(ans.get("insights") or []),
    })

    mark = "" if ok_tool else " <<< 工具不符"
    print(f"{i:<4}{kind:<9}{exp_s:<22}{tool_s:<24}{basis:<26}{dt:>6.1f}s{mark}")

# ── 逐项明细 ──────────────────────────────────────────────
print()
print("=" * 94)
print("回答内容抽样（带结论与结构）")
print("=" * 94)
for c in RESULT["cases"]:
    if c["kind"] in ("query", "explain") and c["i"] % 3 == 1:
        print(f"\n[{c['i']}] {c['q']}")
        print(f"    kind={c['answer_kind']}  实体={c['n_entities']}  "
              f"解读={c['n_insights']}  动作={c['n_actions']}  警示={c['has_warning']}")
        print(f"    结论: {c['headline']}")

# ── 统计 ──────────────────────────────────────────────────
print()
print("=" * 94)
print("质量统计")
print("=" * 94)
n = len(RESULT["cases"])
by_kind = {}
for c in RESULT["cases"]:
    by_kind.setdefault(c["kind"], []).append(c)

print(f"  用例总数 {n}   错误 {len(RESULT['errors'])}")
print()
print(f"  {'类别':<10}{'数量':>5}{'工具命中':>10}{'有幻觉':>8}{'ungrounded':>12}")
for k, items in by_kind.items():
    hit = sum(1 for c in items if c["ok_tool"])
    hal = sum(1 for c in items if c["verify_failed"])
    ung = sum(1 for c in items if c["basis"] == "ungrounded")
    print(f"  {k:<10}{len(items):>5}{hit:>6}/{len(items):<3}{hal:>8}{ung:>12}")

print()
halluc = [c for c in RESULT["cases"] if c["verify_failed"]]
print(f"  幻觉拦截（模型编数字被抓）: {len(halluc)} 例")
for c in halluc:
    print(f"    · [{c['i']}] {c['q']} → 孤儿数字 {c['verify_failed']}")

ung = [c for c in RESULT["cases"] if c["basis"] == "ungrounded"]
print(f"  无依据作答（未查数据）: {len(ung)} 例")
for c in ung:
    print(f"    · [{c['i']}] {c['q']}")

basis_dist = {}
for c in RESULT["cases"]:
    basis_dist[c["basis"]] = basis_dist.get(c["basis"], 0) + 1
print()
print("  口径分布:")
for b, cnt in sorted(basis_dist.items(), key=lambda x: -x[1]):
    print(f"    {b:<26} {cnt}")

# ── 性能 ──────────────────────────────────────────────────
print()
print("=" * 94)
print("性能统计（单线程串行）")
print("=" * 94)
lat = sorted(c["elapsed"] for c in RESULT["cases"])
if lat:
    def pct(p):
        idx = min(int(len(lat) * p), len(lat) - 1)
        return lat[idx]
    print(f"  样本 {len(lat)}")
    print(f"  最小 {lat[0]:6.2f}s   P50 {pct(0.5):6.2f}s   "
          f"P90 {pct(0.9):6.2f}s   最大 {lat[-1]:6.2f}s")
    print(f"  均值 {statistics.mean(lat):6.2f}s   中位 {statistics.median(lat):6.2f}s")
    print(f"  总和 {sum(lat):6.1f}s")

# 按是否调 LLM 分组（拒答不调模型，应显著更快）
guard_lat = [c["elapsed"] for c in RESULT["cases"] if c["basis"] == "guard_blocked"]
llm_lat = [c["elapsed"] for c in RESULT["cases"]
           if c["basis"] not in ("guard_blocked",)]
if guard_lat:
    print(f"\n  Guard 拦截（不调 LLM）: n={len(guard_lat)}  "
          f"均值 {statistics.mean(guard_lat):.2f}s  "
          f"最大 {max(guard_lat):.2f}s")
if llm_lat:
    print(f"  调用 LLM           : n={len(llm_lat)}  "
          f"均值 {statistics.mean(llm_lat):.2f}s  "
          f"P90 {sorted(llm_lat)[int(len(llm_lat)*0.9)]:.2f}s")

# 按工具轮数分组
print()
print("  按工具调用数分组（延迟 vs 轮数）:")
rounds = {}
for c in RESULT["cases"]:
    rounds.setdefault(len(c["calls"]), []).append(c["elapsed"])
for r in sorted(rounds):
    v = rounds[r]
    print(f"    {r} 次工具调用: n={len(v):<3} 均值 {statistics.mean(v):6.2f}s")

# ── 并发 ──────────────────────────────────────────────────
print()
print("=" * 94)
print("并发测试（4 并发，同一批问题）")
print("=" * 94)
CONC_Q = [
    "C034525 这个人要不要打电话？",
    "现在决策阈值是多少？",
    "工单处理得怎么样？",
    "挽回价值最高的3个客户",
]


def one(q):
    d, dt, err = post("/api/agent/ask", {"question": q})
    return {"q": q, "dt": dt, "err": err,
            "basis": (d or {}).get("basis")}


t0 = time.time()
with ThreadPoolExecutor(max_workers=4) as ex:
    conc = list(ex.map(one, CONC_Q))
wall = time.time() - t0

for c in conc:
    tag = c["err"] or c["basis"]
    print(f"  {c['dt']:6.2f}s  {tag:<26} {c['q'][:34]}")
serial = sum(c["dt"] for c in conc)
print(f"\n  墙钟 {wall:.2f}s   各请求之和 {serial:.2f}s   "
      f"并行加速比 {serial/wall if wall else 0:.2f}x")
errs = [c for c in conc if c["err"]]
print(f"  并发错误 {len(errs)} 个")
for e in errs:
    print(f"    · {e['q']}: {e['err'][:100]}")

# ── 数据安全核对 ──────────────────────────────────────────
print()
print("=" * 94)
print("数据安全核对（本脚本不应改库）")
print("=" * 94)
total_after = get("/api/work-orders/stats")
print(f"  total  : {total_before['total']} → {total_after['total']}")
print(f"  pending: {total_before['pending']} → {total_after['pending']}")
if total_before == total_after:
    print("  [OK] 工单数据未被改动")
else:
    print("  [FAIL] 数据被改动了！")
    RESULT["errors"].append("数据被改动")

# ── 落盘 ──────────────────────────────────────────────────
RESULT["latency"] = {
    "all": lat, "guard": guard_lat, "llm": llm_lat,
    "concurrency": [{"q": c["q"], "dt": c["dt"], "err": c["err"]} for c in conc],
    "wall": wall,
}
RESULT["data_ok"] = total_before == total_after
with open("/tmp/batch_result.json", "w", encoding="utf-8") as f:
    json.dump(RESULT, f, ensure_ascii=False, indent=1)

fails = [c for c in RESULT["cases"] if not c["ok_tool"]]
print()
print("=" * 94)
print(f"总结：{n} 个用例，工具/口径不符 {len(fails)} 个，"
      f"错误 {len(RESULT['errors'])} 个，数据安全 {RESULT['data_ok']}")
for c in fails:
    print(f"  · [{c['i']}] {c['q']} → 调用 {c['calls']} basis={c['basis']}")
print("=" * 94)
