"""验证：枚举值中文化（确定性替换）+ 不误伤标识符。

背景：实测模型在中文 insights 里夹带 `CRITICAL` / `HIGH` 这类英文枚举，
读起来割裂。提示词已要求它自己译，但本项目已有三次"提示词不可靠"的教训，
故加确定性替换。

本测试两侧都要：
  · 英文枚举必须被译成中文
  · 客户编号 C071081、字段名 expected_value 等**不得被误伤**
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "backend"))

from app.agent.graph import _cn_enums  # noqa: E402

SHOULD_TRANSLATE = [
    ("三人均为 CRITICAL 风险、HIGH 价值", "三人均为 极高 风险、高危 价值"),
    ("该客户属 ZERO 价值层", "该客户属 零余额 价值层"),
    ("渠道为 relationship", "渠道为 客户经理"),
    ("MEDIUM 与 LOW 等级", "中等 与 低风险 等级"),
    ("建议走 automated", "建议走 自动触达"),
]

SHOULD_NOT_TOUCH = [
    "客户 C071081 期望挽回 22.0 万",
    "字段 expected_value 表示期望价值",
    "概率 0.9757，余额 ¥225,563",
    "ZERO_PRODUCTS 是变量名",
    "这条 C034525 与 C062858 都值得",
    "https://example.com/HIGHER",
]

print("=" * 84)
print("一、英文枚举应被译成中文")
print("=" * 84)
fails = []
for src, want in SHOULD_TRANSLATE:
    got = _cn_enums(src)
    ok = got == want
    if not ok:
        fails.append(f"{src!r} → {got!r}，期望 {want!r}")
    print(f"  [{'OK ' if ok else 'FAIL'}] {src}")
    if not ok:
        print(f"         实得: {got}")

print()
print("=" * 84)
print("二、标识符与数据不得被误伤")
print("=" * 84)
for src in SHOULD_NOT_TOUCH:
    got = _cn_enums(src)
    ok = got == src
    if not ok:
        fails.append(f"误伤 {src!r} → {got!r}")
    print(f"  [{'OK ' if ok else 'FAIL'}] {src}")
    if not ok:
        print(f"         实得: {got}")

print()
print("=" * 84)
print("三、边界")
print("=" * 84)
for src in ["", None]:
    got = _cn_enums(src)
    ok = got == src
    print(f"  [{'OK ' if ok else 'FAIL'}] {src!r} → {got!r}")
    if not ok:
        fails.append(f"边界 {src!r} 失败")

print()
print("=" * 84)
if fails:
    print(f"结论：FAIL —— {len(fails)} 项")
    for f in fails:
        print(f"  · {f}")
else:
    print("结论：PASS —— 枚举中文化生效，且不误伤客户编号与字段名")
