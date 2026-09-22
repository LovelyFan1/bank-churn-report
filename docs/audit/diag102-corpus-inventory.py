"""盘点：项目里**已经存在**的银行业务规则语料有多少。

用户的质疑是「意图识别结合银行的业务规则语料也没办法投喂」。
这个质疑默认了"没有语料"。但本项目其实已经积累了相当量的
业务规则文本 —— 只是它们散落在代码注释与报告里，从未被当作语料看待。

本脚本量化四件事：
  1) 各处语料的实际字符数
  2) 折算 token 量（中文约 1.5 字/token，代码/英文约 4 字/token）
  3) 是否塞得进常见上下文窗口
  4) 其中有"可检索结构"的部分（如一条条规则 vs 大段叙述）
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOURCES = [
    ("backend/app/config.py", "三个业务假设的完整推导（成本比/客单价/挽留成功率）"),
    ("backend/app/services/risk_scoring.py", "两套阈值设计、成本模型、策略表、理由映射"),
    ("backend/app/services/cost_benefit_service.py", "ROI 推导、口径标注、两个 ROI 分离"),
    ("backend/app/services/note_service.py", "建单理由的四段模板与经济性判定"),
    ("backend/app/services/model_service.py", "运行时指标对齐（meta 过时问题）"),
    ("backend/app/routers/work_orders.py", "工单状态机、快照、渠道覆盖规则"),
    ("backend/app/routers/customers.py", "只读预览端点与路由顺序约束"),
    ("docs/audit/DEFECT-REPORT.md", "37 条缺陷的实测依据与业务逻辑评估"),
    ("docker-compose.yml", "部署口径：数据源、worker 数、重启约束"),
]


def count(path):
    full = os.path.join(ROOT, path)
    if not os.path.exists(full):
        return None
    with open(full, encoding="utf-8") as f:
        text = f.read()
    lines = text.count("\n") + 1
    # 中文/全角字符按 1 字计，其余按英文计
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", text))
    other = len(text) - cjk
    # token 估算：CJK 约 1.5 字/token；ASCII 约 4 字/token
    tok = cjk / 1.5 + other / 4
    return {"chars": len(text), "lines": lines, "cjk": cjk, "tok": tok}


print("=" * 96)
print("一、项目内已有的业务规则语料（实测字符数）")
print("=" * 96)
print(f"{'文件':<46}{'行数':>7}{'字符':>9}{'中文字':>9}{'≈token':>9}")
print("-" * 96)

total_tok = 0
total_chars = 0
rows = []
for path, desc in SOURCES:
    r = count(path)
    if r is None:
        print(f"{path:<46}{'缺失':>7}")
        continue
    total_tok += r["tok"]
    total_chars += r["chars"]
    rows.append((path, r, desc))
    print(f"{path:<46}{r['lines']:>7}{r['chars']:>9,}{r['cjk']:>9,}{r['tok']:>9,.0f}")

print("-" * 96)
print(f"{'合计':<46}{'':>7}{total_chars:>9,}{'':>9}{total_tok:>9,.0f}")

print()
print("=" * 96)
print("二、能否塞进上下文窗口（决定是否需要 RAG）")
print("=" * 96)
for win, name in [(8192, "8K（小模型）"), (32768, "32K"),
                  (131072, "128K（GPT-4o/DeepSeek/Qwen 常见）"),
                  (1048576, "1M（Gemini 1.5 Pro）")]:
    pct = total_tok / win * 100
    mark = "塞得进" if pct <= 60 else ("勉强/超限" if pct <= 100 else "超限")
    print(f"  {name:<32} 占用 {pct:6.1f}%   {mark}")

print()
print("  注：60% 是经验阈值 —— 要给对话历史、工具返回、系统指令留余量。")

print()
print("=" * 96)
print("三、哪些部分天然适合 RAG（结构化、可切块）")
print("=" * 96)
RS = os.path.join(ROOT, "backend/app/services/risk_scoring.py")
with open(RS, encoding="utf-8") as f:
    rs = f.read()

# 数一数可切块的规则条目
checks = [
    ("_REASON_MAP 条目（风险因素→理由）",
     len(re.findall(r'^    \(\[.*?\]', rs, re.M))),
    ("_ACTION_TABLE 动作（价值层×等级）",
     len(re.findall(r'"[^"]+": "[^"]+"', rs))),
    ("以 ⚠ 开头的实测说明段",
     len(re.findall(r'⚠', rs))),
    ("以「实测」引出的证据句",
     len(re.findall(r'实测', rs))),
]
for name, n in checks:
    print(f"  {name:<40} {n:>4} 条")

DR = os.path.join(ROOT, "docs/audit/DEFECT-REPORT.md")
with open(DR, encoding="utf-8") as f:
    dr = f.read()
print()
print("  DEFECT-REPORT.md 结构：")
print(f"    一级标题（章节）    {len(re.findall(r'^## ', dr, re.M)):>4} 个")
print(f"    二级标题（小节）    {len(re.findall(r'^### ', dr, re.M)):>4} 个")
print(f"    表格行              {len(re.findall(r'^\|', dr, re.M)):>4} 行")
print(f"    「实测」出现        {len(re.findall(r'实测', dr)):>4} 次")

print()
print("=" * 96)
print("四、结论")
print("=" * 96)
print(f"  可用业务规则语料约 {total_chars:,} 字符 / ≈{total_tok:,.0f} token。")
print()
print("  ⇒ 若用 128K 上下文的模型：**全量塞进系统提示即可**，无需向量库。")
print("  ⇒ 若用 8K/32K 小模型：需要 RAG 或摘要压缩。")
print()
print("  ⇒ 更关键的是语料的**性质**：它不是散乱文档，而是")
print("     「每条规则都带实测依据」的结构化知识 —— 这正是最难得的语料。")
