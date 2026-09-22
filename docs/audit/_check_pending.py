"""检查待确认建单路径的结构化答案是否包含正文。

疑点：改版后前端待确认区只显示了按钮，正文（客户/等级/余额/理由）
不见了。需要确认是后端没给 text，还是前端没渲染。
"""
import io
import json
import urllib.request

BASE = "http://localhost:8000"
body = json.dumps({"question": "帮我给 C034525 建个挽留工单，负责人写张思远"}).encode()
req = urllib.request.Request(BASE + "/api/agent/ask", data=body,
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=180) as r:
    d = json.loads(r.read().decode())

a = d.get("answer") or {}
out = [
    f"basis      : {d.get('basis')}",
    f"kind       : {a.get('kind')}",
    f"headline   : {a.get('headline')}",
    f"text len   : {len(a.get('text') or '')}",
    "text       :",
    (a.get("text") or "(空)"),
    "",
    f"pending_action 存在: {bool(d.get('pending_action'))}",
    f"answer 的键: {sorted(a.keys())}",
]
io.open(r"C:\Users\yyfab\AppData\Local\Temp\pend.txt", "w",
        encoding="utf-8").write("\n".join(str(x) for x in out))
print("已写入 pend.txt")
