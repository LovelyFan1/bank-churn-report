"""核查：智能体落地的前置硬约束。

要回答三个问题，不靠猜：
  1) 后端容器能不能出网？（LLM 调用需要）
  2) 后端有没有装任何 LLM SDK？（没有则需新增依赖 + 重建镜像）
  3) 宿主上的 DSH 能不能出网？（对比参照）

同时核查 Compose 里有没有任何 API key 注入点。
"""
import json
import os
import socket
import urllib.request

print("=" * 78)
print("一、后端容器出网探测")
print("=" * 78)

targets = [
    ("https://api.deepseek.com", "DeepSeek API"),
    ("https://dashscope.aliyuncs.com", "阿里云百炼"),
    ("https://open.bigmodel.cn", "智谱"),
    ("https://www.baidu.com", "百度（通用连通性）"),
]
for url, name in targets:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "probe"})
        with urllib.request.urlopen(req, timeout=6) as r:
            print(f"  [OK ] {name:<18} HTTP {r.status}")
    except Exception as e:
        print(f"  [FAIL] {name:<18} {type(e).__name__}: {str(e)[:60]}")

print()
print("=" * 78)
print("二、DNS 解析")
print("=" * 78)
for host in ["api.deepseek.com", "pypi.org"]:
    try:
        ip = socket.gethostbyname(host)
        print(f"  [OK ] {host:<24} -> {ip}")
    except Exception as e:
        print(f"  [FAIL] {host:<24} {type(e).__name__}: {e}")

print()
print("=" * 78)
print("三、已安装的 LLM 相关 SDK")
print("=" * 78)
for mod in ["openai", "anthropic", "langchain", "dashscope", "zhipuai",
            "httpx", "requests", "aiohttp", "tiktoken", "transformers"]:
    try:
        m = __import__(mod)
        v = getattr(m, "__version__", "?")
        print(f"  [有] {mod:<16} {v}")
    except ImportError:
        print(f"  [无] {mod}")

print()
print("=" * 78)
print("四、环境变量中的 API Key（Compose 是否注入过）")
print("=" * 78)
keys = [k for k in os.environ if any(
    s in k.upper() for s in ["API_KEY", "TOKEN", "OPENAI", "LLM", "DEEPSEEK", "QWEN"])]
if keys:
    for k in keys:
        print(f"  {k} = {str(os.environ[k])[:8]}...")
else:
    print("  （无任何 API Key 环境变量）")

print()
print("=" * 78)
print("五、可用的本地算力（若走「无 LLM」的规则型智能体）")
print("=" * 78)
try:
    import multiprocessing
    print(f"  CPU 核数 {multiprocessing.cpu_count()}")
except Exception:
    pass
try:
    import torch
    print(f"  torch {torch.__version__}  CUDA={torch.cuda.is_available()}")
except ImportError:
    print("  torch 未安装 → 无法本地跑开源大模型")
