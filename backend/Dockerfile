FROM python:3.12-slim

WORKDIR /app

# ── 系统依赖 ─────────────────────────────────────────────
# 安装 ML 库所需的编译工具（gcc/g++ 给 lightgbm/xgboost 用，libgomp1 给
# OpenMP 并行用）。
#
# ⚠ apt 源必须可覆盖（实测缺陷）：默认的 deb.debian.org 在本网络下
#   **SSL 握手超时 / HTTP 超时**（实测 urlopen 15s 超时），导致
#   `docker compose build` 直接失败、且错误信息停在 apt-get 那一行，
#   看起来像 Dockerfile 写错。实测可用的镜像：
#       mirrors.tuna.tsinghua.edu.cn   HTTP 200
#       mirrors.aliyun.com             HTTP 200
#   故把源做成构建参数，默认仍用官方源（不改变原有行为），
#   网络受限时：docker compose build --build-arg APT_MIRROR=... backend
ARG APT_MIRROR=deb.debian.org
RUN set -eux; \
    if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
        # 覆写 debian.sources（trixie 用新格式，兼容旧的 sources.list）
        if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
            sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
        fi; \
        if [ -f /etc/apt/sources.list ]; then \
            sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list; \
        fi; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        g++ \
        libgomp1 \
        && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
# ── 镜像源可覆盖 ─────────────────────────────────────────
# 实测：直连 pypi.org 在本网络下频繁 ReadTimeout（装 langgraph 时
# 反复重试仍失败），改走国内镜像后一次成功。故把源做成可覆盖参数，
# 默认仍用官方源（不改变原有行为），构建慢时可：
#   docker compose build --build-arg PIP_INDEX_URL=<镜像地址> backend
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# Copy backend source code
COPY . .

# Expose FastAPI port
EXPOSE 8000

# Run FastAPI with uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
