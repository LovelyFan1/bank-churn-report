"""读取 pip --dry-run 报告，统计 LangGraph 方案的实际依赖规模。

用于回答一个具体问题：引入 LangGraph 编排要付出多少依赖代价。
不靠印象，读 pip 自己生成的 report.json。
"""
import json
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rep.json"
try:
    rep = json.load(open(path, encoding="utf-8"))
except Exception as e:
    print(f"读取失败 {type(e).__name__}: {e}")
    raise SystemExit(1)

names = sorted({i["metadata"]["name"].lower() for i in rep.get("install", [])})
print(f"新增依赖总数: {len(names)}")
print()
for n in names:
    print(f"  · {n}")
