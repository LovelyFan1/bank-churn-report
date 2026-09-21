"""第三十三轮：全代码库静态缺陷扫描（AST 级）。只读。"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path("/app/app")

# 允许的静默 except（有明确注释说明的除外，这里全列出供人工判断）
findings = {
    "bare_except": [],          # except:  (裸 except)
    "except_pass": [],          # except ...: pass
    "except_return_none": [],   # except: return None
    "mutable_default": [],      # def f(x=[])
    "broad_except": [],         # except Exception
    "no_qa": [],                # TODO/FIXME/XXX/HACK
    "print_in_service": [],     # 服务层用 print 而非 logger
    "os_path_join": [],         # 用 os.path 而非 pathlib（风格，低优先）
    "global_mutation": [],      # 函数内 global 声明
    "division": [],             # 潜在除零（裸 / ）—— 需人工判断
    "sql_raw": [],              # 字符串拼接 SQL
    "hardcoded_threshold": [],  # 硬编码数字阈值
}

for py in sorted(ROOT.rglob("*.py")):
    rel = py.relative_to(ROOT)
    src = py.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"❌ 语法错误 {rel}: {e}")
        continue
    lines = src.splitlines()

    for node in ast.walk(tree):
        # except handlers
        if isinstance(node, ast.ExceptHandler):
            tname = getattr(node.type, "id", None) or getattr(
                getattr(node.type, "attr", None), "__str__", lambda: None)()
            if node.type is None:
                findings["bare_except"].append((rel, node.lineno,
                    lines[node.lineno-1].strip()))
            else:
                tstr = ast.unparse(node.type) if node.type else ""
                if "Exception" in tstr or "BaseException" in tstr:
                    # 检查 body 是否只是 pass / return None
                    body = node.body
                    is_pass = all(isinstance(s, ast.Pass) for s in body)
                    is_ret_none = all(
                        isinstance(s, ast.Return) and
                        (s.value is None or (isinstance(s.value, ast.Constant)
                                             and s.value.value is None))
                        for s in body)
                    if is_pass:
                        findings["except_pass"].append((rel, node.lineno,
                            lines[node.lineno-1].strip()))
                    elif is_ret_none:
                        findings["except_return_none"].append((rel, node.lineno,
                            lines[node.lineno-1].strip()))
                    else:
                        findings["broad_except"].append((rel, node.lineno,
                            lines[node.lineno-1].strip()))

        # mutable default args
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.args.defaults + [x for x in node.args.kw_defaults if x]:
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    findings["mutable_default"].append((rel, node.lineno, node.name))
            # global
            for sub in ast.walk(node):
                if isinstance(sub, ast.Global):
                    findings["global_mutation"].append(
                        (rel, sub.lineno, f"{node.name}: {', '.join(sub.names)}"))

        # 裸除法（粗筛）
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            findings["division"].append((rel, node.lineno,
                lines[node.lineno-1].strip()[:100]))

    # 文本级扫描
    for i, line in enumerate(lines, 1):
        ls = line.strip()
        for tag in ("TODO", "FIXME", "XXX", "HACK"):
            if tag in line and not ls.startswith("#!"):
                findings["no_qa"].append((rel, i, ls[:100]))
        if "print(" in line and "/services/" in str(rel):
            findings["print_in_service"].append((rel, i, ls[:90]))
        if "os.path.join" in line:
            findings["os_path_join"].append((rel, i, ls[:80]))

print("=" * 96)
print("一、静默异常吞噬（最危险的缺陷类型）")
print("=" * 96)
for key, label in [("bare_except", "裸 except:"),
                   ("except_pass", "except ...: pass"),
                   ("except_return_none", "except ...: return None")]:
    items = findings[key]
    print(f"\n  ── {label} ({len(items)}) ──")
    for rel, ln, txt in items:
        print(f"    {rel}:{ln}  {txt[:90]}")

print()
print("=" * 96)
print(f"二、宽泛 except Exception 总数: {len(findings['broad_except'])}")
print("=" * 96)
for rel, ln, txt in findings["broad_except"][:25]:
    print(f"    {rel}:{ln}  {txt[:88]}")

print()
print("=" * 96)
print("三、可变的默认参数（经典陷阱）")
print("=" * 96)
if findings["mutable_default"]:
    for rel, ln, name in findings["mutable_default"]:
        print(f"    ❌ {rel}:{ln} def {name}(...=[]/{{}})")
else:
    print("    ✅ 未发现")

print()
print("=" * 96)
print("四、函数内 global 声明（多进程/多线程下的状态风险）")
print("=" * 96)
for rel, ln, txt in findings["global_mutation"]:
    print(f"    {rel}:{ln}  {txt}")

print()
print("=" * 96)
print("五、TODO/FIXME 标记")
print("=" * 96)
if findings["no_qa"]:
    for rel, ln, txt in findings["no_qa"]:
        print(f"    {rel}:{ln}  {txt}")
else:
    print("    ✅ 无")

print()
print("=" * 96)
print("六、服务层用 print 而非 logging")
print("=" * 96)
if findings["print_in_service"]:
    for rel, ln, txt in findings["print_in_service"][:20]:
        print(f"    {rel}:{ln}  {txt}")
else:
    print("    ✅ 无")

print()
print("=" * 96)
print("七、潜在除零（需人工确认分母是否可能为 0）")
print("=" * 96)
print(f"    共 {len(findings['division'])} 处除法表达式，抽样: ")
seen = set()
for rel, ln, txt in findings["division"]:
    if rel in seen:
        continue
    seen.add(rel)
    print(f"    {rel}:{ln}  {txt}")
