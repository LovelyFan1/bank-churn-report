/**
 * 风险等级统一口径工具。
 *
 * 核心约定：风险等级**只由后端 risk_scoring 决定** —— 它用的是原始概率的
 * 分位数边界（P95 / P70 / P35），不是固定的 0.7 / 0.3 / 0.1。前端任何一处
 * 都不得写死阈值，分级与配色一律以 GET /api/model/risk-info 返回的
 * `thresholds` 为准。设计与理由见 backend/app/services/risk_scoring.py 顶部说明。
 */

// 等级由高到低
export const RISK_LEVELS = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']

const LABELS = { CRITICAL: '极高', HIGH: '高危', MEDIUM: '中等', LOW: '低风险' }
const COLORS = { CRITICAL: '#ef4444', HIGH: '#fb923c', MEDIUM: '#facc15', LOW: '#22d3ee' }

/** 模型未训练时的回退阈值 —— 与后端 risk_scoring._legacy_level 保持一致 */
export const LEGACY_THRESHOLDS = { critical: 0.7, high: 0.3, medium: 0.1 }

/** 统一中文名。此前 Dashboard 的 CRITICAL 叫「极高」、工单页叫「紧急」，现已收敛。 */
export function riskLabel(level) {
  return LABELS[level] || level || '—'
}

export function riskColor(level) {
  return COLORS[level] || '#94a3b8'
}

/** 徽章类名，对应 style.css 中的全局 .risk-badge / .risk-* 定义 */
export function riskBadgeClass(level) {
  return 'risk-' + (level || 'MEDIUM').toLowerCase()
}

function normalize(thresholds) {
  return thresholds && typeof thresholds.critical === 'number'
    ? thresholds
    : LEGACY_THRESHOLDS
}

/**
 * 原始概率 → 风险等级。
 * @param {number} prob 模型原始概率
 * @param {{critical:number,high:number,medium:number}} [thresholds] 来自 /api/model/risk-info
 */
export function levelOf(prob, thresholds) {
  if (prob == null) return 'LOW'
  const t = normalize(thresholds)
  if (prob >= t.critical) return 'CRITICAL'
  if (prob >= t.high) return 'HIGH'
  if (prob >= t.medium) return 'MEDIUM'
  return 'LOW'
}

/**
 * 概率条颜色 —— 由**等级**推导，而不是拿概率去比一组写死的阈值。
 *
 * 这样进度条颜色与紧挨着的徽章文案必然一致；否则会出现「绿色进度条 + 高危徽章」
 * 这种自相矛盾的展示（旧代码里 probColor 写死 0.7/0.3/0.1，正是这个来源）。
 */
export function probColor(prob, thresholds) {
  if (prob == null) return '#94a3b8'
  return riskColor(levelOf(prob, thresholds))
}

/** 概率 → 百分比字符串。全文统一 1 位小数，避免同列数值精度不一。 */
export function fmtPercent(p, digits = 1) {
  if (p == null) return '0%'
  return (p * 100).toFixed(digits) + '%'
}

/**
 * 金额 → 万元字符串（保留整数万）。用于期望价值/挽回金额这类量级表达，
 * 与 fmtPercent 并列。负数与 null 一律显示 '—'。
 */
export function fmtWan(v) {
  if (v == null || !isFinite(v)) return '—'
  return (v / 10000).toLocaleString('zh-CN', { maximumFractionDigits: 0 }) + '万'
}

// ── 客户价值层 ──────────────────────────────────────────
//
// 与风险等级正交的第二个维度：风险等级回答「会不会跑」，价值层回答「跑了值多少」。
// 由后端 risk_scoring.value_tier() 按余额划分，前端不得自行判定 ——
// 与阈值同理，边界（VALUE_TIER_HIGH）属后端业务假设。

const TIER_LABELS = { HIGH: '高价值', LOW: '低价值', ZERO: '零余额' }
const TIER_COLORS = { HIGH: '#a78bfa', LOW: '#60a5fa', ZERO: '#94a3b8' }

/** 触达渠道 —— 与后端 risk_scoring.CHANNEL_BY_TIER 对应 */
const CHANNEL_LABELS = {
  relationship: '客户经理 1 对 1',
  outbound: '主动外呼',
  automated: 'APP 推送 / 短信',
}

export function valueTierLabel(tier) {
  return TIER_LABELS[tier] || tier || '—'
}

export function valueTierColor(tier) {
  return TIER_COLORS[tier] || '#94a3b8'
}

export function channelLabel(channel) {
  return CHANNEL_LABELS[channel] || channel || '—'
}
