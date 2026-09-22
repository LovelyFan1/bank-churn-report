"""检查结构化答案的字段完整性（避免控制台 GBK 编码问题）。"""
import io
import json

d = json.load(io.open(r"C:\Users\yyfab\AppData\Local\Temp\a.json", encoding="utf-8"))
a = d["answer"]

out = []
out.append(f"basis    : {d['basis']}")
out.append(f"kind     : {a.get('kind')}")
out.append(f"headline : {a.get('headline')}")
w = a.get("warning") or {}
out.append(f"warning  : [{w.get('level')}] {w.get('text')}")
out.append(f"           hint: {w.get('hint')}")
out.append("insights :")
for s in a.get("insights") or []:
    out.append(f"  - {s}")
out.append(f"entities : {len(a.get('entities') or [])}")
for e in a.get("entities") or []:
    out.append(f"  {e['customer_id']} {e['surname']}")
    out.append(f"    primary  : {e['primary']['label']} = {e['primary']['value_text']}"
               f" ({e['primary']['value_wan']})")
    out.append(f"    secondary: " + ", ".join(
        f"{s['label']}={s['value']}" for s in e.get("secondary", [])))
    out.append(f"    tags     : {e['tags']}")
    out.append(f"    action   : {e['action']}")
    out.append(f"    has_order={e['has_active_order']}  can_create={e['can_create_order']}")
    out.append(f"    worth    : {e['worthiness'].get('label')} / {e['worthiness'].get('net_text')}")
out.append("actions  :")
for x in a.get("actions") or []:
    out.append(f"  id={x.get('id')} label={x.get('label')}")
    out.append(f"    targets={x.get('targets')} needs_confirm={x.get('needs_confirm')}")
    if x.get("blocked"):
        out.append(f"    blocked={x['blocked']}")
    if x.get("note"):
        out.append(f"    note={x['note']}")
out.append(f"verify_failed: {d.get('verify_failed')}")
out.append(f"tool_calls   : {[c['name'] for c in d.get('tool_calls', [])]}")

io.open(r"C:\Users\yyfab\AppData\Local\Temp\struct_out.txt", "w",
        encoding="utf-8").write("\n".join(out))
print("已写入 struct_out.txt")
