"""共享数据库去重与执行闸门，响应丢失和重复投递均不重复计算。"""
import hashlib
import json
import time
import uuid
from functools import wraps
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from app.models.task_submission import TaskSubmission


def submit(db, user, key, kind, payload, task, model_version=None):
    if not key or not 8 <= len(key) <= 128:
        raise HTTPException(422, '提交任务需要8至128字符的Idempotency-Key')
    row = db.query(TaskSubmission).filter_by(username=user.username, idempotency_key=key).first()
    effective_version = row.model_version if row else model_version
    inputs = {key: value for key, value in payload.items() if key not in ('model_snapshot', 'data_snapshot')}
    fingerprint = hashlib.sha256(json.dumps({'kind': kind, 'payload': inputs,
        'model_version': effective_version}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if row is None:
        row = TaskSubmission(task_id=uuid.uuid4().hex, username=user.username,
            idempotency_key=key, kind=kind, input_hash=fingerprint, model_version=model_version,
            phase='created', lease_until=0, payload_json=json.dumps(payload, ensure_ascii=False, allow_nan=False))
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.query(TaskSubmission).filter_by(username=user.username, idempotency_key=key).one()
    if row.kind != kind or row.input_hash != fingerprint:
        raise HTTPException(409, '同一幂等键不能提交不同任务参数')
    now = time.time()
    claimed = db.query(TaskSubmission).filter(TaskSubmission.task_id == row.task_id,
        TaskSubmission.phase.in_(('created', 'dispatching')), TaskSubmission.lease_until <= now).update(
            {'phase': 'dispatching', 'lease_until': now + 30}, synchronize_session=False)
    db.commit()
    if claimed:
        try:
            task.apply_async(kwargs=json.loads(row.payload_json), task_id=row.task_id)
        except Exception:
            db.query(TaskSubmission).filter_by(task_id=row.task_id, phase='dispatching').update(
                {'phase': 'created', 'lease_until': 0}, synchronize_session=False)
            db.commit()
            raise HTTPException(503, '任务队列暂不可用，使用原幂等键重试')
        db.query(TaskSubmission).filter_by(task_id=row.task_id, phase='dispatching').update(
            {'phase': 'queued', 'lease_until': 0}, synchronize_session=False)
        db.commit()
    return {'task_id': row.task_id, 'status': 'pending', 'model_version': row.model_version,
            'message': '任务已登记；同一幂等键重试会返回相同任务'}


def track_submission(function):
    @wraps(function)
    def tracked(self, *args, **kwargs):
        task_id = self.request.id
        if not task_id:
            return function(self, *args, **kwargs)
        from app.database import SessionLocal
        with SessionLocal() as db:
            row = db.get(TaskSubmission, task_id)
            if row is None:
                return function(self, *args, **kwargs)  # 兼容旧队列任务。
            claimed = db.query(TaskSubmission).filter(TaskSubmission.task_id == task_id,
                TaskSubmission.phase.in_(('created', 'dispatching', 'queued'))).update(
                    {'phase': 'running'}, synchronize_session=False)
            db.commit()
            if not claimed:
                return {'status': 'duplicate_delivery', 'task_id': task_id}
        try:
            result = function(self, *args, **kwargs)
        except Exception:
            result = {'status': 'failed', 'error': '任务执行失败，请查看服务端日志'}
        failed = isinstance(result, dict) and result.get('status') in ('failed', 'timeout')
        with SessionLocal() as db:
            db.query(TaskSubmission).filter_by(task_id=task_id).update(
                {'phase': 'failure' if failed else 'success',
                 'result_json': json.dumps(result, ensure_ascii=False, default=str)}, synchronize_session=False)
            db.commit()
        return result
    return tracked
