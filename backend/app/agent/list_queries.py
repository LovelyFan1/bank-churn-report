"""查询签名只包含业务筛选与排序，分页由服务端上下文决定。"""
import re
from app.agent.quantities import quantity

DEFAULTS = {"sort_by": "expected_value", "exited": 0, "has_order": False, "worth_only": False}
FILTERS = set(DEFAULTS) | {"geography", "risk_level", "value_tier", "min_balance", "max_balance"}
FOLLOWUP = re.compile(r"另外|下一批|下一页|再来|再调|还有吗|继续(?:列出|查询|推荐|调出|找|查|看|下一批)")


def signature(params):
    return {key: params.get(key, DEFAULTS.get(key)) for key in sorted(FILTERS)}


def requested_changes(question):
    changes = {}
    for words, value in ((('France', '法国'), 'France'), (('Germany', '德国'), 'Germany'), (('Spain', '西班牙'), 'Spain')):
        if any(word in question for word in words):
            changes['geography'] = value
    for words, value in ((('极高危', '极高风险'), 'CRITICAL'), (('高危', '高风险'), 'HIGH'), (('中等风险', '中危'), 'MEDIUM'), (('低危', '低风险'), 'LOW')):
        if any(word in question for word in words):
            changes['risk_level'] = value
            break
    for words, value in ((('价值最高', '挽回价值', '期望价值', '风险资产评分'), 'expected_value'), (('概率最高', '按概率', '风险最高'), 'probability'), (('余额最高', '按余额'), 'balance'), (('按年龄',), 'age'), (('按信用评分',), 'credit_score')):
        if any(word in question for word in words):
            changes['sort_by'] = value
    if '未流失' in question or '没有流失' in question:
        changes['exited'] = 0
    elif '已流失' in question:
        changes['exited'] = 1
    if any(word in question for word in ('没有工单', '没有进行中工单', '没有在途工单', '未建单')):
        changes['has_order'] = False
    elif '已有工单' in question:
        changes['has_order'] = True
    if '值得' in question and any(word in question for word in ('人工', '投入', '挽留')):
        changes['worth_only'] = True
    return changes


def continuation(previous, question, proposed=None):
    params = {key: value for key, value in previous.items() if key in FILTERS | {'limit'}}
    params.update({key: value for key, value in (proposed or {}).items() if key in FILTERS | {'limit'}})
    params.update(requested_changes(question))
    size = quantity(question)
    if size is not None:
        params['limit'] = size
    changed = signature(params) != signature(previous)
    params['offset'] = 0 if changed else previous.get('next_offset', 0)
    if not changed:
        params['exclude_ids'] = previous.get('seen_customer_ids') or []
        params['expected_version'] = previous.get('model_version')
    return params, changed
