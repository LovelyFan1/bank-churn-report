"""核查：接主流智能体框架/LLM 的现实可行性。

我上一轮建议"零新增依赖"，理由是后端连 requests 都没装。
但那个判断只回答了"现状是什么"，没回答"能不能装、装了能不能通"。
本脚本把这两件事测清楚，避免我用现状代替可行性。

测四件事：
  1) 容器内能否 pip 安装（有无网络、有无权限、镜像是否可写）
  2) 境外 LLM 端点是否可达（主流框架默认面向 OpenAI/Anthropic）
  3) 国内 LLM 端点是否可达（作为替代）
  4) 代理是否可用（宿主机 git 走 7897 成功过）
"""
import json
import os
import socket
import subprocess
import sys
import urllib.request

print("=" * 80)
print("零、环境")
print("=" * 80)
print(f"  Python {sys.version.split()[0]}")
print(f"  是否有 pip: ", end="")
try:
    import pip
    print(f"有 ({pip.__version__})")
except ImportError:
    print("无")

print()
print("=" * 80)
print("一、pip 能否安装（关键：决定能否用主流框架）")
print("=" * 80)
try:
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run",
         "--no-deps", "--quiet", "langchain-core"],
        capture_output=True, text=True, timeout=180,
    )
    print(f"  退出码 {r.returncode}")
    if r.returncode == 0:
        print("  [可以] pip 安装可行 —— 主流框架技术上装得上")
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines()[-6:]:
        print("   ", line[:110])
except Exception as e:
    print(f"  [失败] {type(e).__name__}: {e}")

print()
print("=" * 80)
print("二、境外 LLM 端点（主流框架默认面向）")
print("=" * 80)
overseas = [
    ("https://api.openai.com/v1/models", "OpenAI"),
    ("https://api.anthropic.com/v1/models", "Anthropic"),
    ("https://generativelanguage.googleapis.com", "Google Gemini"),
    ("https://api.mistral.ai", "Mistral"),
    ("https://api.groq.com", "Groq"),
]
for url, name in overseas:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "probe"})
        with urllib.request.urlopen(req, timeout=8) as r:
            print(f"  [可达] {name:<18} HTTP {r.status}")
    except urllib.error.HTTPError as e:
        # 401/403 = 网络通、仅缺 key —— 这就算"可达"
        print(f"  [可达] {name:<18} HTTP {e.code}（网络通，仅缺 key）")
    except Exception as e:
        print(f"  [不可达] {name:<18} {type(e).__name__}: {str(e)[:50]}")

print()
print("=" * 80)
print("三、国内 LLM 端点")
print("=" * 80)
domestic = [
    ("https://api.deepseek.com/v1/models", "DeepSeek"),
    ("https://dashscope.aliyuncs.com/api/v1", "阿里百炼"),
    ("https://open.bigmodel.cn/api/paas/v4", "智谱 GLM"),
    ("https://aip.baidubce.com", "百度千帆"),
    ("https://api.moonshot.cn/v1/models", "月之暗面 Kimi"),
]
for url, name in domestic:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "probe"})
        with urllib.request.urlopen(req, timeout=8) as r:
            print(f"  [可达] {name:<14} HTTP {r.status}")
    except urllib.error.HTTPError as e:
        print(f"  [可达] {name:<14} HTTP {e.code}（网络通，仅缺 key）")
    except Exception as e:
        print(f"  [不可达] {name:<14} {type(e).__name__}: {str(e)[:50]}")

print()
print("=" * 80)
print("四、代理可用性（宿主机 git 曾走 127.0.0.1:7897）")
print("=" * 80)
for proxy in ["http://host.docker.internal:7897", "http://172.17.0.1:7897"]:
    try:
        handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        opener = urllib.request.build_opener(handler)
        with opener.open("https://api.openai.com/v1/models", timeout=8) as r:
            print(f"  [通] {proxy} HTTP {r.status}")
    except urllib.error.HTTPError as e:
        print(f"  [通] {proxy} HTTP {e.code}（网络通，仅缺 key）")
    except Exception as e:
        print(f"  [不通] {proxy} {type(e).__name__}: {str(e)[:50]}")

print()
print("=" * 80)
print("五、宿主机注入到容器的环境变量里有无 key")
print("=" * 80)
hits = [k for k in os.environ
        if any(s in k.upper() for s in
               ["API_KEY", "APIKEY", "OPENAI", "ANTHROPIC", "DEEPSEEK",
                "QWEN", "GLM", "ZHIPU", "MOONSHOT", "LLM", "TOKEN"])]
print(f"  {hits if hits else '（无任何 LLM 相关环境变量）'}")
