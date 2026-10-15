export const AGENT_BATCH_LIMIT = 20

export function batchPlan(targets) {
  const requested = [...new Set((targets || []).map(id => String(id).trim().toUpperCase()).filter(Boolean))]
  return { requested, current: requested.slice(0, AGENT_BATCH_LIMIT), remaining: requested.slice(AGENT_BATCH_LIMIT) }
}
