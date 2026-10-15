STATUS_LABELS = {'pending':'待处理', 'in_progress':'处理中', 'completed':'已完成', 'lost':'已流失', 'cancelled':'已取消'}
RESULT_LABELS = {'retained':'已标记挽留', 'lost':'已标记流失'}


def status_label(value):
    return STATUS_LABELS.get(value, f'未知状态（{value}）' if value else '未记录')


def result_label(value):
    return RESULT_LABELS.get(value, f'未知结果（{value}）' if value else '未记录')
