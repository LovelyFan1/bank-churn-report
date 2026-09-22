#!/bin/sh
# 探测哪个 Debian 镜像可达 —— 用于修复 Dockerfile 的 apt 源。
# 背景：实测 deb.debian.org 在构建时 Connection failed（IP 151.101.90.132:80），
# 导致 docker compose build 失败，进而 langgraph 依赖装不进镜像。
for m in mirrors.tuna.tsinghua.edu.cn mirrors.aliyun.com mirrors.ustc.edu.cn deb.debian.org; do
  if timeout 10 wget -q --spider "http://$m/debian/dists/trixie/Release" 2>/dev/null; then
    echo "REACHABLE   $m"
  else
    echo "unreachable $m"
  fi
done
