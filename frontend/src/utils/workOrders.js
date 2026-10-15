export const STATUS_LABELS = Object.freeze({ pending: '待处理', in_progress: '处理中', completed: '已完成', lost: '已流失', cancelled: '已取消' })
export const STATUS_OPTIONS = Object.keys(STATUS_LABELS)
export const RESULT_LABELS = Object.freeze({ retained: '已标记挽留', lost: '已标记流失' })
export function statusLabel(value) { return STATUS_LABELS[value] || (value ? `未知状态（${value}）` : '未记录') }
export function resultLabel(value) { return RESULT_LABELS[value] || (value ? `未知结果（${value}）` : '未记录') }
