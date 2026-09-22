"""探测 Debian 镜像可达性 —— 用 urllib（确认存在）而非 wget。

发现：python:3.12-slim 内很可能**没有 wget**，导致前两次探测
全部 "unreachable" —— 那是"命令不存在"，不是"网络不通"。
故改用本镜像确定存在的 python urllib 重测。
"""
import urllib.request

TARGETS = [
    ("https://deb.debian.org/debian/dists/trixie/Release", "deb.debian.org HTTPS"),
    ("http://deb.debian.org/debian/dists/trixie/Release", "deb.debian.org HTTP"),
    ("https://mirrors.tuna.tsinghua.edu.cn/debian/dists/trixie/Release", "tuna HTTPS"),
    ("http://mirrors.tuna.tsinghua.edu.cn/debian/dists/trixie/Release", "tuna HTTP"),
    ("https://mirrors.aliyun.com/debian/dists/trixie/Release", "aliyun HTTPS"),
    ("https://pypi.tuna.tsinghua.edu.cn/simple/", "pypi.tuna（已知可用，对照）"),
]

for url, name in TARGETS:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "probe"})
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"  [OK  ] {name:<34} HTTP {r.status}")
    except Exception as e:
        print(f"  [FAIL] {name:<34} {type(e).__name__}: {str(e)[:60]}")
