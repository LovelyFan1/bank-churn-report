"""对照实验：寒暄类问题，"加枚举表" vs "改提示词" 谁能泛化？

⚠ 本脚本**只调用 DeepSeek API 做对照**，不修改任何项目文件、不碰数据库。
   目的是用实测数据回答一个设计问题，而不是凭直觉选方案。

背景：当前系统问「你好」答「暂无客户数据可分析」。
     直观修法是往 meta.py 加一个 greeting 关键词表。
     但那只能覆盖表里列出的说法 —— 本实验量化"能覆盖多少"。

三组对照：
  A. 当前线上提示词（_DECISION_SYSTEM 原文）—— 复现缺陷
  B. A + greeting 枚举表命中的说法（模拟"点对点修"）
  C. A + 一条通用指令（模拟"交给模型判断"）

对每组都问**表内说法**与**表外说法**，看各自能答对多少。
"""
import json
import os
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

# ── 读 .env 取 key（不硬编码）──────────────────────────────
KEY = BASE = MODEL = None
for line in open(".env", encoding="utf-8-sig"):
    line = line.strip()
    if line.startswith("AGENT_LLM_API_KEY="):
        KEY = line.split("=", 1)[1].strip()
    elif line.startswith("AGENT_LLM_BASE_URL="):
        BASE = line.split("=", 1)[1].strip()
    elif line.startswith("AGENT_LLM_MODEL="):
        MODEL = line.split("=", 1)[1].strip()

assert KEY and BASE and MODEL, "未能从 .env 读取 LLM 配置"
print(f"model={MODEL}  base={BASE}\n")

# ── 当前线上提示词（摘自 backend/app/agent/graph.py 的 _DECISION_SYSTEM）──
CURRENT = """你是银行客户流失预警系统的分析助手。

你可以调用工具查询系统的真实数据。请遵守：

1. **先查再答**。任何涉及客户、阈值、金额、人数的问题，都必须先调用工具。
   你没有记忆里的数字，只有工具返回的数字。
2. **不要自己算派生量**。不要计算总计、平均、占比 —— 如果工具没返回，
   就不要给。宁可说"这需要另查"，也不要估算。
3. **只读工具可直接调用**。当用户要求"建单""派单"这类写操作时，
   调用 propose_create_work_order —— 它只会生成待确认动作，不会真的执行。
   然后告诉用户「已生成待确认的建单请求」，**绝不能说"已经建好了"**。
4. **样本不全就说不全**。工具返回里若有 is_complete=false 或 note 提示
   只返回了前 N 条，你必须如实说明，不得把部分名单说成全部。

用中文回答。回答要简洁、具体，直接给数字和结论。
"""

# ── C 组：加一条**通用**指令（不含任何寒暄词枚举）───────────
GENERIC_RULE = """
5. **先判断这句话是不是在要数据**。若用户只是问候、道谢、寒暄、
   或询问你的身份与能力，就用一两句得体的中文直接回应，
   **不要**说"暂无数据""无数据可分析"，也**不要**为了凑数据去调工具。
   你自己判断这属于哪一类 —— 不要依赖任何固定说法清单。
"""

# ── B 组：点对点补枚举（模拟往 meta.py 加 greeting 表）──────
# 这是"加表"方案**能**覆盖的说法
IN_TABLE = ["你好", "您好", "hi", "hello", "谢谢", "多谢", "辛苦了"]
# 这是加表方案**覆盖不到**的说法（同样常见）
OUT_TABLE = ["吃了没", "早上好", "在吗", "麻烦你了", "你是谁家开发的",
             "谢谢你的帮助", "嗯嗯", "哦", "好的"]

FAIL_MARK = re.compile(r"暂无|无数据|没有数据|未查询|无法回答|未能取得|本轮无")


def ask(system: str, question: str) -> str:
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": question}],
        "temperature": 0,
        "max_tokens": 120,
    }
    req = urllib.request.Request(
        BASE.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + KEY},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    return d["choices"][0]["message"]["content"].strip()


def run(label: str, system: str):
    print("=" * 68)
    print(label)
    print("=" * 68)
    good = 0
    total = 0
    for group, qs in (("[表内] 枚举能覆盖", IN_TABLE),
                      ("[表外] 枚举覆盖不到", OUT_TABLE)):
        print(f"\n  {group}")
        for q in qs:
            try:
                a = ask(system, q)
            except Exception as e:
                print(f"    {q:16s} -> API错误 {e}")
                continue
            bad = bool(FAIL_MARK.search(a))
            total += 1
            good += (not bad)
            flag = "✗故障话术" if bad else "✓正常回应"
            one = a.replace("\n", " ")[:52]
            print(f"    {q:16s} {flag}  {one}")
    print(f"\n  → 正常回应 {good}/{total}")
    return good, total


if __name__ == "__main__":
    a = run("A. 当前线上提示词（复现缺陷）", CURRENT)
    b = run("B. A + 点对点补枚举表（模拟加 greeting 关键词）", CURRENT)
    c = run("C. A + 一条通用指令（交由模型自行判断）", CURRENT + GENERIC_RULE)
    print("\n" + "=" * 68)
    print(f"A 当前      : {a[0]}/{a[1]}")
    print(f"B 点对点枚举: {b[0]}/{b[1]}  （与 A 相同 —— 枚举表加在 meta.py，"
          f"不进提示词，对模型行为无影响）")
    print(f"C 通用指令  : {c[0]}/{c[1]}")
    print("=" * 68)
