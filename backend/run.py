"""启动脚本 — FastAPI 开发服务器。

Celery Worker 需要单独启动:
    celery -A app.celery_app worker --loglevel=info --concurrency=2 -Q default -n worker1

Redis 必须在本机运行:
    redis-server       (Linux/Mac)
    redis-server.exe   (Windows: https://github.com/tporadowski/redis/releases)

启动顺序:
    1. Redis
    2. Celery Worker
    3. FastAPI (本脚本)
"""

import os

import uvicorn

if __name__ == "__main__":
    # ⚠ reload 与 workers 互斥 —— uvicorn 会打印
    #   `WARNING: "workers" flag is ignored when reloading is enabled.`
    #   并**静默忽略 workers**。本项目提升并发只能靠多进程（受 GIL 限制，
    #   实测线程加速比仅 0.99x，见 config.WEB_WORKERS 的说明），
    #   所以二者必须二选一，由环境变量切换：
    #
    #     RELOAD=true  → 开发模式：单进程 + 热重载
    #     默认         → 多进程模式：WEB_WORKERS 个进程，改代码需重启容器
    reload = os.getenv("RELOAD", "false").lower() == "true"
    workers = int(os.getenv("WEB_WORKERS", "1"))

    if reload:
        uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
    else:
        uvicorn.run("app.main:app", host="0.0.0.0", port=8000, workers=workers)
