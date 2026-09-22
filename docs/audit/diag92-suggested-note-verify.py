"""验证：建单建议理由（suggested-note）四档措辞 + 数字自洽。

**为什么必须四档都测**
note_service 有四条分支（no_asset / not_worth / marginal / worth），
只测一个客户无法证明另外三条不写错。本脚本按经济性四档各取样本，
逐句核对：
  1) 句中金额与 risk_scoring 独立算出的值一致（不信任 note 自己）
  2) 量纲统一：所有比较都用「期望可挽回 vs 单次干预成本」
  3) 不含未经模型支持的声明（如"自动化渠道更省钱"）
  4) 口径标注存在
"""
import json
import re
import urllib.request

BASE = "http://localhost:8000"
AVG = 50000.0
CR = 5.0
S = 0.30
C = AVG / CR          # 单次干预成本 10000
BE = C / S            # 平衡点 33333.33


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=300) as r:
        return json.loads(r.read().decode())


# ── 1. 采集四个分档的样本 ────────────────────────────────
print("=" * 84)
print("一、按经济性四档采集样本")
print("=" * 84)

buckets = {"no_asset": [], "not_worth": [], "marginal": [], "worth": []}


def _classify(bal, p):
    if bal <= 0:
        return "no_asset"
    ratio = (p * bal) / BE
    return "worth" if ratio >= 1 else ("marginal" if ratio >= 0.5 else "not_worth")


def _absorb(items, limit=3):
    for c in items:
        k = _classify(c["balance"] or 0, c["probability"] or 0)
        if len(buckets[k]) < limit:
            buckets[k].append(c)


# 主扫描：按概率降序（覆盖决策线内，主要出 no_asset / marginal / worth）
page = 1
while page <= 120:
    d = get(f"/api/customers?page={page}&page_size=100&sort_by=probability&sort_order=desc")
    if not d["items"]:
        break
    _absorb(d["items"])
    if all(len(v) >= 3 for v in buckets.values()):
        break
    if page * 100 >= d["total"]:
        break
    page += 1

# 补充扫描：挑 not_worth。
# ⚠ 首次运行时 not_worth 采到 0 个 —— 因为上面按概率降序，前 12000 人
#   都是高概率客户，而 not_worth 需要「有余额但余额太低」。
#   改用 value_tier=LOW + balance 升序，低余额客户排在最前，必中该档。
if len(buckets["not_worth"]) < 3:
    for order in ("asc", "desc"):
        if len(buckets["not_worth"]) >= 3:
            break
        page = 1
        while page <= 5:
            d = get(f"/api/customers?page={page}&page_size=100&value_tier=LOW"
                    f"&sort_by=balance&sort_order={order}")
            if not d["items"]:
                break
            _absorb(d["items"])
            if len(buckets["not_worth"]) >= 3:
                break
            page += 1

for k, v in buckets.items():
    print(f"  {k:<12} 采到 {len(v)} 个")

print()
print("=" * 84)
print("二、逐档验证")
print("=" * 84)

fails = []
for k, samples in buckets.items():
    if not samples:
        fails.append(f"{k}: 未采到样本，该分支未被验证")
        continue
    c = samples[0]
    r = get(f"/api/customers/{c['customer_id']}/suggested-note")
    note = r["note"]
    w = r["worthiness"]
    comp = r["components"]

    print()
    print(f"── 分支 {k}  客户 {c['customer_id']} " + "─" * 30)
    print(f"   判定      {w['verdict']}   ratio={w['ratio']}")
    print(f"   理由      {note[:200]}{'…' if len(note) > 200 else ''}")

    # 校验 1：verdict 与独立计算一致
    bal, p = c["balance"] or 0, c["probability"] or 0
    ev_authoritative = c["expected_value"]     # 权威 EV（由未舍入概率算得）
    exp = _classify(bal, p)
    if w["verdict"] != exp:
        fails.append(f"{k}/{c['customer_id']}: verdict={w['verdict']} 期望 {exp}")
    else:
        print(f"   [OK] verdict 与独立计算一致")

    # 校验 2：individual_net_profit 自洽 = EV×s − c（EV 取权威 expected_value）
    expect_net = ev_authoritative * S - C
    if abs(w["net"] - expect_net) > 0.01:
        fails.append(f"{k}: net={w['net']} 期望 {expect_net:.2f}")
    else:
        print(f"   [OK] 净收益自洽  {w['net']:,.2f}")

    # 校验 3：句中出现的期望可挽回金额
    #
    # ⚠ 首次运行时这里报了 0.1 元级差异（8325.41 vs 8325.31），查清了根因，
    #   确认**不是 note_service 的错**，而是一处更底层的舍入口径瑕疵：
    #     _build_scored_result 里  evs = np.round(p_unrounded * bal, 2)
    #                             probs = np.round(p_unrounded, 4)
    #   即 expected_value 由**未舍入**的概率算出，而对外返回的 probability
    #   舍入到 4 位。用户拿显示值复算 p×bal 会与 expected_value 差一点。
    #   实测量级 0.1 元 / 8325 元 ≈ 0.001%，不影响任何决策，故不改动
    #   （改它会波及全量打分的每一个数字，风险远大于收益）。
    #   这里把校验锚定到**真正的不变量**：rec == round(expected_value × s, 2)。
    #   同时量化该舍入差异并报告，不掩盖。
    rec = comp["expected_recoverable"]
    expect_rec = round(ev_authoritative * S, 2)
    if abs(rec - expect_rec) > 0.01:
        fails.append(f"{k}: expected_recoverable={rec} 期望 {expect_rec}")
    else:
        print(f"   [OK] 期望可挽回自洽  {rec:,.2f}")

    naive = p * bal * S
    gap = abs(rec - naive)
    note_gap = f"（与 p×balance 复算差 {gap:,.2f} 元，舍入口径，非错误）" if gap > 0.01 else ""
    print(f"   [--] p×bal 复算={naive:,.2f} 差 {gap:,.2f} 元 {note_gap}")

    if f"¥{rec:,.0f}" not in note:
        fails.append(f"{k}: 理由中未出现期望可挽回金额 ¥{rec:,.0f}")
    else:
        print(f"   [OK] 金额已写入理由")

    # 校验 4：口径标注存在
    if "口径" not in note or "假设" not in note:
        fails.append(f"{k}: 缺口径标注")
    else:
        print(f"   [OK] 含口径标注")

    # 校验 5：不得出现未经模型支持的渠道成本声明
    for bad_phrase in ["成本更低", "省钱", "零成本", "低成本渠道"]:
        if bad_phrase in note:
            fails.append(f"{k}: 出现无模型依据的措辞「{bad_phrase}」")
    print(f"   [OK] 无未经验证的渠道成本声明")

    # 校验 5b：不得出现 Markdown 标记
    # ⚠ note 是**纯文本**字段：它显示在 <textarea> 与工单列表里，不走 Markdown
    #   渲染。实测踩过：模板里写了 **加粗**，界面上原样显示星号 —— 读者看到
    #   的是"**值得投入**"，比不加粗更糟。故设为回归红线。
    if "**" in note or note.count("`") > 0:
        fails.append(f"{k}: note 含 Markdown 标记（纯文本字段，会原样显示星号/反引号）")
    else:
        print(f"   [OK] 无 Markdown 标记（纯文本字段）")

    # 校验 6：量纲 —— 若出现平衡点金额，必须同时出现"期望价值"限定词
    if f"¥{BE:,.0f}" in note.replace("33,333", f"{BE:,.0f}"):
        if "期望价值" not in note:
            fails.append(f"{k}: 提到平衡点但未说明它定义在期望价值上")
        else:
            print(f"   [OK] 平衡点已标注量纲")

print()
print("=" * 84)
print("三、反向核对：note 里所有金额都能在 components 中找到出处")
print("=" * 84)
r = get("/api/customers/C000001/suggested-note")
note = r["note"]
amounts = set(int(x.replace(",", "")) for x in re.findall(r"¥([\d,]+)", note))
allowed = {
    int(r["components"]["balance"]), int(r["components"]["average_customer_value"])
    if "average_customer_value" in r["components"] else 50000,
    int(r["components"]["expected_recoverable"]),
    int(r["components"]["cost_per_intervention"]),
    int(r["components"]["breakeven_expected_value"]),
}
# 允许等于净收益绝对值
allowed.add(int(abs(r["components"]["individual_net_profit"])))
allowed.add(50000)
orphan = [a for a in amounts if not any(abs(a - x) <= 1 for x in allowed)]
print(f"  理由中出现的金额：{sorted(amounts)}")
print(f"  允许的出处集合  ：{sorted(allowed)}")
if orphan:
    print(f"  [FAIL] 无法溯源的金额：{orphan}")
    fails.append(f"存在无法溯源的金额 {orphan}")
else:
    print(f"  [OK] 全部金额均可溯源")

print()
print("=" * 84)
if fails:
    print(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        print(f"  · {f}")
else:
    print("结论：PASS —— 四档措辞、数字自洽、量纲统一、可溯源 全部通过")
print("=" * 84)
