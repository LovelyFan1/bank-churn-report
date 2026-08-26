"""通用任务状态查询路由 — 适配所有 Celery 异步任务。"""

from fastapi import APIRouter
from celery.result import AsyncResult
from app.celery_app import celery_app

router = APIRouter(prefix="/api/tasks", tags=["Tasks"])


@router.get("/{task_id}")
async def get_task_status(task_id: str):
    """查询任意 Celery 任务的状态和结果。

    返回格式:
        - pending:   {"status": "pending", "result": null}
        - running:   {"status": "PROGRESS", "result": {"step": "...", "message": "..."}}
        - completed: {"status": "SUCCESS", "result": {...}}
        - failed:    {"status": "FAILURE", "result": {"error": "..."}}
    """
    result = AsyncResult(task_id, app=celery_app)

    response = {"task_id": task_id, "status": result.state}

    if result.state == "PENDING":
        response["result"] = None
    elif result.state == "PROGRESS":
        response["result"] = result.info  # 进度信息
    elif result.state == "SUCCESS":
        response["result"] = result.result
    elif result.state == "FAILURE":
        response["result"] = {"error": str(result.info)}
    else:
        response["result"] = str(result.info) if result.info else None

    return response
