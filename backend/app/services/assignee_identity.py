"""负责人稳定身份与受控历史别名，不用相似姓名扩大工单权限。"""
from fastapi import HTTPException
from sqlalchemy import or_, and_, func
from app.models.user import User
from app.models.work_order import WorkOrder
from app.services import auth_service as auth
from app.services import auth_deps


def aliases(db, user):
    values = {user.username}
    if user.display_name:
        matches = db.query(User.username).filter(User.display_name == user.display_name).all()
        if len(matches) == 1 and matches[0][0] == user.username:
            values.add(user.display_name)
    return values


def matches(order, user, db):
    if order.assignee_username:
        return order.assignee_username == user.username
    return order.assignee in aliases(db, user)


def predicate(db, value):
    user = db.query(User).filter(User.username == value).first()
    if user is None:
        names = db.query(User).filter(User.display_name == value).limit(2).all()
        user = names[0] if len(names) == 1 else None
    if user is None:
        return and_(WorkOrder.assignee_username.is_(None), WorkOrder.assignee == value)
    return or_(WorkOrder.assignee_username == user.username,
        and_(WorkOrder.assignee_username.is_(None), WorkOrder.assignee.in_(list(aliases(db, user)))))


def resolve(db, value):
    user = db.query(User).filter(User.username == value).first()
    if user is None:
        matches = db.query(User).filter(User.display_name == value).limit(2).all()
        if len(matches) != 1:
            raise HTTPException(422, '负责人不存在或同名，请选择明确工号')
        user = matches[0]
    if not user.is_active or not (auth.has_perm(user.role, auth.PERM_ORDER_WRITE) or auth.has_perm(user.role, auth.PERM_ORDER_ASSIGNED)):
        raise HTTPException(422, '负责人已停用或没有处理工单权限')
    return user


def preview(db):
    rows = db.query(WorkOrder.assignee, func.count(WorkOrder.id)).filter(
        WorkOrder.assignee_username.is_(None), WorkOrder.assignee.isnot(None)).group_by(WorkOrder.assignee).all()
    result = []
    for value, count in rows:
        exact = db.query(User).filter(User.username == value).first()
        users = [exact] if exact else db.query(User).filter(User.display_name == value).all()
        result.append({'original': value, 'count': count,
            'status': 'unique' if len(users) == 1 else 'ambiguous' if users else 'unmatched',
            'username': users[0].username if len(users) == 1 else None})
    return result


def apply_preview(db, plan, actor):
    current = preview(db)
    if current != plan:
        raise HTTPException(409, '映射或工单已变化，请重新生成预览')
    changes = []
    for entry in plan:
        if entry['status'] != 'unique':
            continue
        orders = db.query(WorkOrder).filter(WorkOrder.assignee == entry['original'], WorkOrder.assignee_username.is_(None)).all()
        for order in orders:
            order.assignee_username = entry['username']
            order.assignee_name_snapshot = order.assignee
            changes.append({'order_id': order.id, 'original': order.assignee, 'username': entry['username']})
    auth_deps.write_audit(db, action='assignee_identity_backfill', user=actor, source='migration',
        detail={'mapping': changes}, success=True, message='按已核对的唯一映射补负责人身份，原值保留')
    return changes
