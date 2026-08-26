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

import uvicorn

if __name__ == "__main__":
    # 生产部署时去掉 reload=True，并使用 --workers 多进程
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,  # 仅开发
    )
