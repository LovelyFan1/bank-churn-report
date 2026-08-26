"""Celery 应用配置 — 独立于 FastAPI 进程运行。

启动 Worker:
    celery -A app.celery_app worker --loglevel=info --concurrency=2 -Q default

启动 Flower 监控 (可选):
    celery -A app.celery_app flower
"""

from celery import Celery
from app.config import settings

celery_app = Celery(
    "bank_churn",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.celery_tasks.train", "app.celery_tasks.predict", "app.celery_tasks.cluster"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_soft_time_limit=1800,  # 30 分钟软超时
    task_time_limit=3600,       # 1 小时硬超时
    task_acks_late=True,        # 任务完成后才确认，防止 worker 崩溃丢任务
    worker_prefetch_multiplier=1,  # 每次只取一个任务，适合长任务
    result_expires=86400,       # 结果保留 24 小时
)
