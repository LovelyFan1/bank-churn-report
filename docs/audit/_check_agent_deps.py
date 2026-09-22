"""检查 Agent 依赖是否装齐 —— 供部署后自检。"""
import importlib.util as u
import sys

MODS = ["langgraph", "langchain_core", "langchain_openai", "openai", "tiktoken"]
missing = []
for m in MODS:
    ok = u.find_spec(m) is not None
    print(f"  [{'已装' if ok else '未装'}] {m}")
    if not ok:
        missing.append(m)

print()
if missing:
    print(f"缺失 {len(missing)} 个: {', '.join(missing)}")
    print("→ 需重建镜像或 pip install -r requirements.txt")
    sys.exit(1)
print("全部已装")
