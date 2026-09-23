"""统计 agent 层的「手写枚举表」规模 —— 用来说明点对点修复的根因。

只读脚本，不修改任何文件。
"""
import glob
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

total = 0
rows = []
for f in sorted(glob.glob("backend/app/agent/*.py")):
    src = open(f, encoding="utf-8").read()
    n = 0
    names = []
    for m in re.finditer(r"^_([A-Z][A-Z0-9_]*)\s*=\s*[\[\{]", src, re.M):
        start = m.end() - 1
        depth = 0
        i = start
        while i < len(src):
            if src[i] in "[{":
                depth += 1
            elif src[i] in "]}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = src[start:i + 1]
        # 数表里的字面量条目（中英文词/短语）
        items = len(re.findall(r'"[^"]+"' + r"|'[^']+'", body))
        n += items
        names.append(f"{m.group(1)}({items})")
    if n:
        rows.append((f.split("\\")[-1], n, names))
        total += n

for name, n, names in sorted(rows, key=lambda r: -r[1]):
    print(f"{name:20s} {n:4d} 条")
print("-" * 60)
print(f"{'合计':20s} {total:4d} 条")
print()
print("明细：")
for name, n, names in sorted(rows, key=lambda r: -r[1]):
    print(f"  {name}")
    for x in names:
        print(f"      {x}")
