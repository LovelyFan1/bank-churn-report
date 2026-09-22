"""量化：接主流框架/LLM SDK 的实际代价（依赖规模）。

不靠印象。用 pip --dry-run 看真实会拉进多少包，
并区分"轻量 SDK"与"重框架"两条路线的差异。
"""
import subprocess
import sys

TARGETS = [
    ("openai", "OpenAI 官方 SDK（含原生 function calling）"),
    ("dashscope", "阿里百炼官方 SDK"),
    ("zhipuai", "智谱官方 SDK"),
    ("httpx", "纯 HTTP 客户端（自己实现 function calling）"),
    ("langchain", "LangChain（重框架）"),
    ("langchain-openai", "LangChain 的 OpenAI 适配层"),
    ("llama-index-core", "LlamaIndex 核心"),
]


def count_deps(pkg):
    """返回 (新增包数, 是否成功, 包名列表前若干)"""
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
         "--report", "-", pkg],
        capture_output=True, text=True, timeout=600,
    )
    if r.returncode != 0:
        return None, False, (r.stderr or "")[-200:]
    try:
        import json
        rep = json.loads(r.stdout)
        names = sorted({i["metadata"]["name"].lower()
                        for i in rep.get("install", [])})
        return len(names), True, names
    except Exception as e:
        return None, False, f"{type(e).__name__}: {e}"


print("=" * 82)
print("各方案的实际依赖规模（pip --dry-run 实测）")
print("=" * 82)
print(f"{'包':<22}{'新增依赖数':>10}   说明")
print("-" * 82)

results = {}
for pkg, desc in TARGETS:
    n, ok, extra = count_deps(pkg)
    results[pkg] = (n, ok, extra)
    if ok:
        print(f"{pkg:<22}{n:>10}   {desc}")
    else:
        print(f"{pkg:<22}{'失败':>10}   {desc}")
        print(f"{'':22}   {str(extra)[:100]}")

print()
print("=" * 82)
print("LangChain 到底多装了什么（与 openai 的差集）")
print("=" * 82)
n_lc, ok_lc, lc = results.get("langchain", (None, False, []))
n_oa, ok_oa, oa = results.get("openai", (None, False, []))
if ok_lc and ok_oa:
    only_lc = sorted(set(lc) - set(oa))
    print(f"  langchain 独有 {len(only_lc)} 个包：")
    for x in only_lc[:40]:
        print(f"    · {x}")
    if len(only_lc) > 40:
        print(f"    … 另 {len(only_lc)-40} 个")

print()
print("=" * 82)
print("结论")
print("=" * 82)
if ok_oa and ok_lc:
    print(f"  轻量 SDK（openai）     新增 {n_oa} 个包")
    print(f"  重框架（langchain）    新增 {n_lc} 个包")
    print(f"  倍数                    {n_lc/max(n_oa,1):.1f}x")
