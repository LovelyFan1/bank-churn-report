"""读取批量测试结果 JSON，输出质量与性能报告。

（避免依赖容器 stdout —— 重定向在 Windows 宿主下路径不一致。）
"""
import io
import json
import statistics

p = r"C:\Users\yyfab\AppData\Local\Temp\batch_result.json"
d = json.load(io.open(p, encoding="utf-8"))
cases = d["cases"]
out = []


def p_(s=""):
    out.append(str(s))


p_("=" * 92)
p_("一、逐用例结果")
p_("=" * 92)
p_(f"{'#':<4}{'类别':<9}{'工具/口径':<30}{'basis':<24}{'耗时':>8}")
p_("-" * 92)
for c in cases:
    calls = ",".join(x.replace("get_", "").replace("propose_", "")[:14]
                     for x in c["calls"]) or "-"
    flag = "" if c["ok_tool"] else "  <<<"
    p_(f"{c['i']:<4}{c['kind']:<9}{calls:<30}{c['basis']:<24}{c['elapsed']:>7.2f}s{flag}")

p_()
p_("=" * 92)
p_("二、质量统计")
p_("=" * 92)
by = {}
for c in cases:
    by.setdefault(c["kind"], []).append(c)

p_(f"  {'类别':<10}{'数量':>5}{'工具/口径命中':>14}{'编数字被捕':>12}{'未查数据':>10}")
for k, items in by.items():
    hit = sum(1 for c in items if c["ok_tool"])
    hal = sum(1 for c in items if c["verify_failed"])
    ung = sum(1 for c in items if c["basis"] == "ungrounded")
    p_(f"  {k:<10}{len(items):>5}{f'{hit}/{len(items)}':>14}{hal:>12}{ung:>10}")

p_()
p_("  口径分布:")
dist = {}
for c in cases:
    dist[c["basis"]] = dist.get(c["basis"], 0) + 1
for b, n in sorted(dist.items(), key=lambda x: -x[1]):
    p_(f"    {b:<26} {n}")

hal = [c for c in cases if c["verify_failed"]]
p_()
p_(f"  模型编数字被拦截: {len(hal)} 例")
for c in hal:
    p_(f"    · [{c['i']}] {c['q']} → {c['verify_failed']}")

ung = [c for c in cases if c["basis"] == "ungrounded"]
p_(f"  未查数据即作答(ungrounded): {len(ung)} 例")
for c in ung:
    p_(f"    · [{c['i']}] {c['q']}")

p_()
p_("  结构化答案完整性:")
for c in cases:
    if c["kind"] in ("query", "explain"):
        p_(f"    [{c['i']:>2}] kind={str(c['answer_kind']):<16} 实体={c['n_entities']:<3} "
           f"解读={c['n_insights']}  动作={c['n_actions']}  警示={str(c['has_warning']):<5} "
           f"| {(c['headline'] or '')[:40]}")

p_()
p_("=" * 92)
p_("三、性能")
p_("=" * 92)
lat = sorted(c["elapsed"] for c in cases)
p_(f"  样本 {len(lat)}")
p_(f"  最小 {lat[0]:.2f}s   P50 {lat[len(lat)//2]:.2f}s   "
   f"P90 {lat[int(len(lat)*0.9)]:.2f}s   最大 {lat[-1]:.2f}s")
p_(f"  均值 {statistics.mean(lat):.2f}s   总和 {sum(lat):.1f}s")

guard = [c["elapsed"] for c in cases if c["basis"] == "guard_blocked"]
llm = [c["elapsed"] for c in cases if c["basis"] != "guard_blocked"]
p_()
if guard:
    p_(f"  Guard 拦截（零 LLM 调用）: n={len(guard):<3} "
       f"均值 {statistics.mean(guard):.2f}s  最大 {max(guard):.2f}s")
if llm:
    s = sorted(llm)
    p_(f"  调用 LLM          : n={len(llm):<3} "
       f"均值 {statistics.mean(llm):.2f}s  P50 {s[len(s)//2]:.2f}s  "
       f"P90 {s[int(len(s)*0.9)]:.2f}s  最大 {s[-1]:.2f}s")

p_()
p_("  按工具调用数（延迟 vs 轮数）:")
r = {}
for c in cases:
    r.setdefault(len(c["calls"]), []).append(c["elapsed"])
for k in sorted(r):
    v = r[k]
    p_(f"    {k} 次: n={len(v):<3} 均值 {statistics.mean(v):6.2f}s  "
       f"范围 {min(v):.2f}~{max(v):.2f}s")

p_()
p_("=" * 92)
p_("四、并发（4 并发）")
p_("=" * 92)
for c in d["latency"]["concurrency"]:
    p_(f"  {c['dt']:6.2f}s  {(c['err'] or 'OK'):<20} {c['q'][:36]}")
p_(f"\n  墙钟 {d['latency']['wall']:.2f}s")

p_()
p_("=" * 92)
p_(f"数据安全: {'未被改动 ✔' if d['data_ok'] else '被改动 ✗'}")
p_(f"不符用例: {len([c for c in cases if not c['ok_tool']])} / {len(cases)}")
p_("=" * 92)

io.open(r"C:\Users\yyfab\AppData\Local\Temp\batch_report.txt", "w",
        encoding="utf-8").write("\n".join(out))
print("written")
