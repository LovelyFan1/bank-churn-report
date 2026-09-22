"""数字保全校验 —— 本项目「不幻觉」的机械保障。

**为什么这是节点而不是提示词**

在提示词里写「不得编造数字」是祈祷。LLM 会在长文本里不知不觉引入
未给出的数值（尤其是"总计""平均"这类它自己想算的派生量）。
本模块把这件事变成**可执行、可复现的检查**：抽取回答中的全部数字，
逐一要求能在工具返回值里找到出处，找不到就丢弃这段回答、回退模板。

**换算白名单（关键，否则会大量误杀）**

实测（diag103）：一个完全正确的回答被判「3 个数字无法溯源」：

    标记为孤儿的        真相
    1. 2.               Markdown 有序列表序号，根本不是数字
    30                  来自 success_rate: 0.3，模型写成「30%」

即**同源换算**必须被允许。白名单涵盖：
    比例 → 百分比   ×100        （0.3 → 30）
    百分比 → 比例   ÷100        （30% → 0.3）
    千分位          1,234 → 1234
    四舍五入        196,212.34 → 196212
    万元换算        ÷10000
    比率取整        33,333.33 → 33333

**设计取舍：宁可漏放，不可误杀**

误杀一个正确回答（用户看到降级模板）比漏放一个编造数字后果轻 ——
但要尽量少误杀，否则用户会频繁遇到"回答被系统丢弃"，体验很差。
故白名单按"合理换算"尽量放宽；真正的孤儿数字（凭空出现的）仍会被抓。

⚠ 本模块**只做数值比对，不做语义判断**。若 LLM 把 0.60 说成"决策阈值是
  0.55"，那是数值错误，本模块能抓（55/100 不在白名单内）；
  若它说"阈值较高"，那是措辞，不在本模块职责范围。
"""

import re
from typing import Any

# 回答中的数字：支持千分位、小数、负数
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# 这些数字是**排版产物**，不是数据，必须忽略：
#   1. 2. 3.          有序列表序号
#   （1）（2）          中文序号
#   第 1 点             序号
#   | 1 | / | 2 |      Markdown 表格的排名/序号列（见下方专门处理）
_ORDINAL_PATTERNS = [
    re.compile(r"(?m)^\s*\d+[.、)]\s"),          # 行首 "1. " / "2、"
    re.compile(r"[（(]\s*\d+\s*[)）]"),           # （1）(2)
    re.compile(r"第\s*\d+\s*[点条款项个步]"),       # 第 1 点
]

# Markdown 表格的**序号列**表头关键词。
# 这些列的单元格是 1/2/3 这样的行号，不是数据。
_ORDINAL_HEADER_KW = ("排名", "序号", "编号", "序", "#", "no.", "no", "index")

# 匹配一个表格单元："| 2 " 或 "| 2 |"
_TABLE_CELL_RE = re.compile(r"\|\s*(\d{1,3})\s*(?=\|)")


def _strip_table_ordinals(text: str) -> str:
    """剥离 Markdown 表格里**序号列**的数字。

    ⚠ 为什么必须单独处理（实测误杀 diag111）：
      模型回答「挽回价值最高的 3 个客户」时用表格列出，Rank 列写
      `| 1 |`、`| 2 |`、`| 3 |`。原实现只剥离行首 `1. ` 形式，
      表格单元格的序号被当成数据 → `2` 不在工具返回值里 → 整段回答
      被判「含无法溯源的数字」并丢弃。

      更糟的是这个误杀**不稳定**：`1` 恰好因 round(0.8883)=1 混进
      白名单、`3` 因工具参数 limit=3 混进白名单，只有 `2` 被抓 ——
      同样正确的回答，有时放过有时丢弃，取决于巧合。

    策略（两条取并集，宁多剥序号也不误杀正确回答）：
      1. 表头某单元格含「排名/序号/#」等词 → 该列所有单元格视为序号
      2. 无表头可辨时，剥离**行首第一个**独立数字单元格（行首 `| N |`），
         且仅当 N ≤ 99（行号范围）——金额/概率不会以这种形式出现在首列

    ⚠ 不无差别剥离所有 `| N |`：若表格中间某列是数据（如「产品数 2」），
      剥离它会削弱校验。故限定在「表头标明的序号列」与「行首列」。
    """
    lines = text.split("\n")
    out = []
    ordinal_cols: set[int] = set()

    for line in lines:
        s = line.strip()
        is_row = s.startswith("|") and s.count("|") >= 2

        if is_row and set(s) <= set("|-: "):
            out.append(line)                      # 分隔行 |---|---|
            continue

        if is_row:
            cells = [c.strip() for c in s.strip("|").split("|")]

            # 识别表头：含序号关键词的列
            if any(kw in c.lower() for c in cells for kw in _ORDINAL_HEADER_KW):
                for i, c in enumerate(cells):
                    if any(kw in c.lower() for kw in _ORDINAL_HEADER_KW):
                        ordinal_cols.add(i)
                out.append(line)
                continue

            # 剥离已识别的序号列
            for i in ordinal_cols:
                if i < len(cells) and re.fullmatch(r"\d{1,3}", cells[i]):
                    cells[i] = "·"
            # 剥离行首的排名列（无表头可辨时）
            if 0 not in ordinal_cols and cells and re.fullmatch(r"\d{1,2}", cells[0]):
                cells[0] = "·"
            out.append("| " + " | ".join(cells) + " |")
            continue

        out.append(line)

    return "\n".join(out)


def _strip_ordinals(text: str) -> str:
    """把序号类数字替换掉，避免它们被当成数据。

    ⚠ 替换成等长空白而非删除，保持其余文本位置不变（便于调试时对照）。
    """
    out = _strip_table_ordinals(text)
    for pat in _ORDINAL_PATTERNS:
        out = pat.sub(lambda m: " " * len(m.group(0)), out)
    return out


def _collect_numbers(obj: Any, into: set) -> None:
    """递归收集工具返回值里的所有数字（含字符串里的数字）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            # 键名里的数字也算（如 "ratio_30"），但一般不出现，保守起见也收
            _collect_numbers(v, into)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, into)
    elif isinstance(obj, bool):
        return                       # True/False 不是数字
    elif isinstance(obj, (int, float)):
        into.add(float(obj))
    elif isinstance(obj, str):
        for m in _NUM_RE.findall(obj):
            try:
                into.add(float(m.replace(",", "")))
            except ValueError:
                pass


def _expand_allowed(values: set[float]) -> set[float]:
    """把允许的数值集合按换算关系扩张。

    ⚠ 扩张必须**有条件的**，不能对所有值一律 ×100/÷100/×10000/÷10000。
      初版无差别扩张，实测出两个严重问题：

        1) 10000.0 经 ×100 得到 1,000,000 → 吞掉了编造的「余额 999,999」
        2) 4821 经 ÷10000 得到 0.4821 → 污染了 0~1 概率区间

      即"盲目扩张"会让白名单大到什么都放行，校验器形同虚设。

    正确的条件规则（按数值本身所属的合理量纲判断）：
      · 0 < v < 1    → 很可能是概率/比率。允许 ×100（→百分比）、
                       以及 1 位小数与整百分数（0.8099 → 81.0 / 81）
      · 1 ≤ v ≤ 100  → 可能是百分比或小计数。允许 ÷100（→比例）
      · v > 100      → 很可能是金额/人数。允许 ÷10000（→万元）、
                       以及千分位导致的舍入（本身已在容差内）
      · 任何情况都允许四舍五入到整数/1位/2位小数（展示需要）

    这样既覆盖真实换算，又不制造跨量纲的假匹配。
    """
    out: set[float] = set()
    for v in values:
        av = abs(v)
        out.add(v)
        out.add(abs(v))
        # 展示层舍入（普遍需要，与量纲无关）
        out.add(round(v))
        out.add(round(v, 1))
        out.add(round(v, 2))
        out.add(round(v, 4))

        if 0 < av < 1:
            # 概率/比率 → 百分比展示
            out.add(v * 100)                 # 0.3 → 30
            out.add(round(v * 100, 1))       # 0.8099 → 81.0
            out.add(round(v * 100))          # 0.8099 → 81
        elif 1 <= av <= 100:
            # 百分比 → 比例（30 → 0.3）
            out.add(v / 100)
            out.add(round(v / 100, 4))
        else:
            # 大数（金额/人数）→ 万元换算
            out.add(v / 10000)               # 220080 → 22.008
            out.add(round(v / 10000, 2))     # → 22.01
            out.add(round(v / 10000, 1))     # → 22.0
    return out


def _found_in_registry(num: float, allowed: set[float]) -> bool:
    """判断一个数字是否能在允许集合中找到（含容差匹配）。

    ⚠ 容差设计是本模块最容易写错的地方，踩过一次坑，务必理解：

    初版用 `max(|a| * 0.005, 1.0)` —— 绝对下限 1.0。
    后果：**任何 0~1 之间的两个数都被判为相等**。
    实测漏放：「该客户余额 999,999 元，流失概率 0.99」中，
      0.99 与真值 0.0 的差是 0.99 < 1.0 → 判为"可溯源"。
    而 0~1 恰好是**本系统最重要的区间**（概率、比率、阈值全在这里），
    等于把校验器在最需要它的地方关掉了。

    正确做法：容差必须**随数量级缩放**，且对小值不设绝对下限。
      · 相对容差 0.5%：覆盖四舍五入与浮点展示误差
      · 对 |v| < 1 的值再加一个"末位精度"容差（如 0.9757 的展示值 0.98
        差 0.0043，占 0.44%，已在相对容差内）
      · **不设绝对下限** —— 小值就用小容差
    """
    if num in allowed:
        return True
    for a in allowed:
        if a == 0:
            # 零只能匹配零（相对容差对 0 无意义）
            if num == 0:
                return True
            continue
        # 相对容差 0.5%，但对极小值限制到 1e-6 以免除零/过严
        tol = max(abs(a) * 0.005, 1e-6)
        if abs(a - num) <= tol:
            return True
    return False


def verify(draft: str, tool_results: list[dict]) -> tuple[bool, list[str]]:
    """校验回答中的数字是否都能从工具返回值溯源。

    返回 (是否通过, 无法溯源的数字字符串列表)。

    ⚠ 若 tool_results 为空（例如纯知识性回答），则**跳过校验**并返回通过 ——
      因为没有数据来源可比对，误判为幻觉反而会阻断正常回答。
      但这种情况下 answer_basis 会被标为无来源，由上层决定是否提示用户。
    """
    if not draft or not draft.strip():
        return True, []

    sources: set[float] = set()
    for r in tool_results or []:
        _collect_numbers(r, sources)

    if not sources:
        return True, []

    allowed = _expand_allowed(sources)
    text = _strip_ordinals(draft)

    orphans: list[str] = []
    for raw in _NUM_RE.findall(text):
        try:
            num = float(raw.replace(",", ""))
        except ValueError:
            continue
        # 忽略纯序号残留（如已剥离但仍有 `1` 单独成句首）
        if not _found_in_registry(num, allowed):
            orphans.append(raw)

    return (len(orphans) == 0), orphans


def explain(orphans: list[str]) -> str:
    """把孤儿数字拼成给用户看的说明 —— 降级时必须如实告知。"""
    if not orphans:
        return ""
    uniq = sorted(set(orphans))
    return (
        "⚠ 本次模型回答中含有无法从系统数据溯源的数字"
        f"（{', '.join(uniq)}），已按「不幻觉」原则丢弃该回答，"
        "改为展示系统计算的确定性结论。"
    )
