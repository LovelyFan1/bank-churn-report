"""通用任务状态查询路由 — 适配所有 Celery 异步任务。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.user import User
from app.models.task_submission import TaskSubmission
import json
from app.services import auth_deps, auth_service as auth
from celery.result import AsyncResult
from app.celery_app import celery_app

router = APIRouter(prefix="/api/tasks", tags=["Tasks"],
                   dependencies=[Depends(auth_deps.require_perm(auth.PERM_MODEL_TRAIN))])


@router.get("/{task_id}")
async def get_task_status(task_id: str, db: Session = Depends(get_db),
                          user: User = Depends(auth_deps.current_user)):
    """查询任意 Celery 任务的状态和结果。

    返回格式:
        - pending:   {"status": "pending", "result": null}
        - running:   {"status": "PROGRESS", "result": {"step": "...", "message": "..."}}
        - completed: {"status": "SUCCESS", "result": {...}}
        - failed:    {"status": "FAILURE", "result": {"error": "..."}}
    """
    submission = db.get(TaskSubmission, task_id)
    if submission:
        if submission.username != user.username and user.role != 'admin':
            raise HTTPException(404, '任务不存在')
        if submission.phase in ('success', 'failure'):
            return {'task_id': task_id, 'status': 'SUCCESS' if submission.phase == 'success' else 'FAILURE',
                    'result': json.loads(submission.result_json or '{}'), 'model_version': submission.model_version}
        if submission.phase != 'running':
            return {'task_id': task_id, 'status': 'PENDING', 'result': None}
    result = AsyncResult(task_id, app=celery_app)

    response = {"task_id": task_id, "status": result.state}
    if submission and submission.phase == 'running' and result.state != 'PROGRESS':
        return {'task_id': task_id, 'status': 'STARTED', 'result': None}

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
