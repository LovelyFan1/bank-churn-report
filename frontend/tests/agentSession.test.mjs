import test from 'node:test'
import assert from 'node:assert/strict'
import { reconcileAgentSession } from '../src/utils/agentSession.js'

const current = () => ({ sessionId: 'window-a', sessionVersion: 3,
  context: { customers: [{ id: 'C071081' }], orders: [{ order_id: 56 }], focus_customer_id: 'C071081' } })

test('confirmation replaces the old focus with authoritative server context', () => {
  const state = current()
  const context = { customers: [{ id: 'C000001' }], orders: [{ order_id: 57 }], focus_customer_id: 'C000001' }
  const result = reconcileAgentSession(state, { session_id: 'window-a', session_version: 4, context })
  assert.equal(result.sessionVersion, 4)
  assert.deepEqual(result.context, context)
  assert.equal(state.context.focus_customer_id, 'C071081')
})

test('bulk confirmation keeps the server cleared focus and all resulting orders', () => {
  const context = { customers: [], orders: [{ order_id: 57 }, { order_id: 58 }], focus_customer_id: null }
  const result = reconcileAgentSession(current(), { session_id: 'window-a', session_version: 5, context })
  assert.equal(result.context.focus_customer_id, null)
  assert.equal(result.context.orders.length, 2)
})

test('late responses and responses from a cleared or different session cannot restore stale focus', () => {
  const state = current()
  for (const [session_id, session_version] of [['window-a', 2], ['old-session', 10]]) {
    assert.equal(reconcileAgentSession(state, { session_id, session_version,
      context: { customers: [], orders: [], focus_customer_id: 'C000001' } }), state)
  }
})

test('missing authority fields cannot manufacture a local focus from an order receipt', () => {
  const state = current()
  for (const response of [{ order: { id: 57, customer_id: 'C000001' } },
    { session_id: 'window-a', session_version: 4, context: { orders: [] } }]) {
    assert.equal(reconcileAgentSession(state, response), state)
  }
})

test('an explicitly expired server lookup clears cached context and resets its version', () => {
  const state = current()
  const response = { session_id: 'window-a', session_version: 0, expired: true }
  assert.equal(reconcileAgentSession(state, response), state)
  assert.deepEqual(reconcileAgentSession(state, response, { allowExpired: true }),
    { sessionId: 'window-a', sessionVersion: 0, context: { customers: [], orders: [] } })
})
