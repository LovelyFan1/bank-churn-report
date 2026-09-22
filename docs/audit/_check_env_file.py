"""检查 .env 是否可被 Docker Compose 正确解析。

背景（实测缺陷）：PowerShell 的 Set-Content -Encoding UTF8 会写入 BOM，
而 Docker Compose 按字面逐行读 .env —— BOM 会被并入**第一个键名**，
变成 "\ufeffAGENT_ENABLED"，导致该变量静默失效（不报错、值为空）。
本项目第一个键恰好是 AGENT_ENABLED，后果是 Agent 永远显示未启用。

本脚本检查：BOM、键名合法性、换行符、key 是否存在。
"""
import os
import re

path = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), ".env")

if not os.path.exists(path):
    print("FAIL: .env 不存在")
    raise SystemExit(1)

raw = open(path, "rb").read()
print(f"文件大小 {len(raw)} 字节")
print(f"BOM      : {'有（会导致首键失效！）' if raw[:3] == b'\xef\xbb\xbf' else '无 ✔'}")
print(f"换行符   : {'CRLF' if b'\\r\\n' in raw else 'LF'}")

text = raw.decode("utf-8-sig")
print()
print("键值对解析：")
ok = True
for i, line in enumerate(text.splitlines(), 1):
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
    if not m:
        print(f"  [{i}] !! 无法解析: {line[:60]}")
        ok = False
        continue
    k, v = m.group(1), m.group(2)
    shown = v if not k.endswith("API_KEY") else (v[:6] + "***" if v else "(空)")
    print(f"  [{i}] {k} = {shown}")
    if k != k.strip():
        ok = False

print()
for need in ["AGENT_ENABLED", "AGENT_LLM_API_KEY", "AGENT_LLM_BASE_URL", "AGENT_LLM_MODEL"]:
    present = re.search(rf"^{need}=", text, re.M) is not None
    print(f"  [{'OK ' if present else 'FAIL'}] 含 {need}")
    if not present:
        ok = False

print()
print("结论：" + ("PASS —— .env 可被 Compose 正确解析" if ok else "FAIL"))
