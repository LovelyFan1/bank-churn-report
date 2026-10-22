/** Only a matching server session may replace the local context. */
export function reconcileAgentSession(current, response, { allowExpired = false } = {}) {
  if (response?.session_id !== current.sessionId) return current
  if (allowExpired && response.expired === true) {
    return { ...current, sessionVersion: 0, context: { customers: [], orders: [] } }
  }
  if (!Number.isInteger(response.session_version)
      || response.session_version < current.sessionVersion
      || !response.context || typeof response.context !== 'object'
      || !Array.isArray(response.context.customers) || !Array.isArray(response.context.orders)) {
    return current
  }
  return { ...current, sessionVersion: response.session_version, context: response.context }
}
