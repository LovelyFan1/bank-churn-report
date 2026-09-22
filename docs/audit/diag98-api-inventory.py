"""列出全部 API 路径与参数 —— 为设计对话页的"工具集"提供事实清单。

对话页的每个意图最终都要落到一个已存在的接口上。若接口不存在，
该意图就不能做（而不是"先做着看"）。故先把清单打全。
"""
import json
import urllib.request

BASE = "http://localhost:8000"

with urllib.request.urlopen(BASE + "/openapi.json", timeout=60) as r:
    d = json.loads(r.read().decode())

paths = d["paths"]
print("=" * 84)
print(f"全部路径 {len(paths)} 个")
print("=" * 84)

for p in sorted(paths):
    for m, spec in sorted(paths[p].items()):
        if m not in ("get", "post", "put", "delete"):
            continue
        params = spec.get("parameters", [])
        required = [q["name"] for q in params if q.get("required")]
        optional = [q["name"] for q in params if not q.get("required")]
        line = f"  {m.upper():<6} {p:<46}"
        if required:
            line += f" 必填={required}"
        if optional:
            line += f" 可选={len(optional)}个"
        print(line)

print()
print("=" * 84)
print("各端点参数细节（只看 GET 且带参数的，这些是对话页可能调用的）")
print("=" * 84)
for p in sorted(paths):
    if "get" not in paths[p]:
        continue
    params = paths[p]["get"].get("parameters", [])
    if not params:
        continue
    print(f"\n{p}")
    for q in params:
        sch = q.get("schema", {})
        t = sch.get("type") or sch.get("anyOf") or "?"
        if isinstance(t, list):
            t = "/".join(x.get("type", "?") for x in t)
        enum = sch.get("enum")
        dflt = sch.get("default")
        bits = [f"type={t}"]
        if enum:
            bits.append(f"enum={enum}")
        if dflt is not None:
            bits.append(f"default={dflt}")
        star = "*" if q.get("required") else " "
        print(f"   {star} {q['name']:<18} {' '.join(bits)}")
