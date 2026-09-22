#!/bin/sh
# 探测 Debian 镜像（HTTPS）—— 实测 80 端口全不可达，而 pip 走 443 成功，
# 故怀疑只有 80 被阻断。本脚本验证 HTTPS 是否可用。
for m in mirrors.tuna.tsinghua.edu.cn mirrors.aliyun.com mirrors.ustc.edu.cn deb.debian.org; do
  if timeout 12 wget -q --spider "https://$m/debian/dists/trixie/Release" 2>/dev/null; then
    echo "HTTPS OK    $m"
  else
    echo "HTTPS fail  $m"
  fi
done
echo "--- pypi 对照（已知可用）---"
if timeout 12 wget -q --spider "https://pypi.tuna.tsinghua.edu.cn/simple/" 2>/dev/null; then
  echo "HTTPS OK    pypi.tuna"
else
  echo "HTTPS fail  pypi.tuna"
fi
