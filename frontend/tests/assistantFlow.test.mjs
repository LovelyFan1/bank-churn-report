import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import { ref, computed } from 'vue'
import { batchPlan } from '../src/utils/agentBatch.js'
import { reconcileAgentSession } from '../src/utils/agentSession.js'

// Exercise the actual page handlers with lifecycle/storage effects disabled.
// No running server, browser credentials or business database are used.
const source = readFileSync(new URL('../src/views/Assistant.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const setup = descriptor.scriptSetup.content.replace(/^import .+$/gm, '')
const instantiate = new Function('deps', `const { ref, computed, onMounted, nextTick, watch, useRoute,
  api, staffList, canAssign, loadAssignees, defaultAssignee, batchPlan,
  reconcileAgentSession, riskLabel, statusLabel } = deps;
  ${setup}
  return { turns, agentContext, sessionId, sessionVersion, openBatch, submitBatch,
    confirmAction, cancelAction, clearSession, closeBatch, openCancel, send, sessionBusy, question, onComposerKeydown };`)

function page(post) {
  const app = instantiate({ ref, computed, onMounted() {}, watch() {}, useRoute: () => ({ query: {} }),
    nextTick: () => Promise.resolve(), api: { post }, staffList: ref([]), canAssign: ref(true),
    loadAssignees() {}, defaultAssignee: () => 'tester', batchPlan, reconcileAgentSession,
    riskLabel: value => value, statusLabel: value => value })
  app.sessionId.value = 'flow-test'
  app.sessionVersion.value = 1
  app.agentContext.value = { customers: [{ id: 'C071081' }],
    orders: [{ order_id: 56, customer_id: 'C071081' }], focus_customer_id: 'C071081' }
  return app
}

test('historical customer-card create synchronizes server context before the next question', async () => {
  const requests = []
  const updated = { customers: [{ id: 'C000001' }],
    orders: [{ order_id: 57, customer_id: 'C000001' }], focus_customer_id: 'C000001' }
  let app
  app = page(async (url, body) => {
    requests.push({ url, body: JSON.parse(JSON.stringify(body)) })
    if (url === '/agent/prepare-create') return { data: {
      session_id: 'flow-test', session_version: 2, context: app.agentContext.value,
      items: [{ customer_id: 'C000001', assignee: 'tester', confirmation_token: 'prepared-b' }], failed: [] } }
    if (url === '/agent/confirm-batch') return { data: {
      session_id: 'flow-test', session_version: 3, context: updated,
      succeeded: 1, created: [{ id: 57, customer_id: 'C000001' }], failed: [] } }
    if (url === '/agent/ask') return { data: {
      session_id: 'flow-test', session_version: 4, context: updated,
      answer: { kind: 'text', text: '请确认目标工单' } } }
    throw new Error(`Unexpected endpoint ${url}`)
  })
  app.turns.value.push({ answer: { entities: [] } })
  const turn = app.turns.value[0]
  await app.openBatch(turn, { targets: ['C000001'] })
  assert.equal(turn.batch.error, '')
  await app.submitBatch(turn)
  assert.equal(turn.batch.error, '')
  assert.equal(app.sessionVersion.value, 3)
  assert.equal(app.sessionBusy.value, false)
  assert.deepEqual(app.agentContext.value, updated)
  await app.send('取消该工单')
  assert.deepEqual(requests.map(r => r.body.session_version), [1, 2, 3])
  assert.ok(requests.every(r => r.body.session_id === 'flow-test'))
  assert.deepEqual(requests[1].body.proposals[0], {
    action: 'create_work_order', confirmation_token: 'prepared-b',
    payload: { customer_id: 'C000001', assignee: 'tester' } })
  assert.deepEqual(requests[2].body.context, updated)
})

test('multi-order cancellation advances each version and keeps batch focus cleared', async () => {
  const requests = []
  let version = 1
  const context = { customers: [], orders: [], focus_customer_id: null }
  const app = page(async (url, body) => {
    requests.push({ url, body: JSON.parse(JSON.stringify(body)) })
    assert.equal(body.session_version, version)
    version += 1
    if (url === '/agent/prepare-order-cancel') return { data: {
      session_id: 'flow-test', session_version: version, context,
      action: 'update_work_order', confirmation_token: `cancel-${body.order_id}`,
      payload: { order_id: body.order_id, customer_id: body.customer_id, status: 'cancelled' },
      current: { customer_name: body.customer_id, status: 'in_progress' } } }
    if (url === '/agent/confirm') {
      const order = { id: body.payload.order_id, customer_id: body.payload.customer_id, status: 'cancelled' }
      context.orders.push({ order_id: order.id, customer_id: order.customer_id, status: order.status })
      return { data: { session_id: 'flow-test', session_version: version,
        context: structuredClone(context), order } }
    }
    throw new Error(`Unexpected endpoint ${url}`)
  })
  app.turns.value.push({ answer: { entities: [
    { order_id: 56, customer_id: 'C071081' }, { order_id: 57, customer_id: 'C000001' }] } })
  const turn = app.turns.value[0]
  await app.openCancel(turn, { targets: [56, 57] })
  await app.submitBatch(turn)
  assert.equal(app.sessionVersion.value, 5)
  assert.equal(app.agentContext.value.focus_customer_id, null)
  assert.equal(app.agentContext.value.orders.length, 2)
  assert.deepEqual(requests.slice(2).map(r => r.body.context_mode), ['batch_start', 'batch'])
  assert.equal(turn.batch.result.succeeded, 2)
})

test('abandoning a proposal blocks confirmation and clearing until revocation finishes', async () => {
  const requests = []
  let finish
  const app = page((url, body) => {
    requests.push({ url, body })
    return new Promise(resolve => { finish = resolve })
  })
  app.turns.value.push({ answer: { entities: [] }, pending: {
    action: 'update_work_order', payload: { order_id: 56, status: 'cancelled' }, confirmation_token: 'cancel-56' } })
  const turn = app.turns.value[0]
  const revoking = app.cancelAction(turn)
  assert.equal(app.sessionBusy.value, true)
  await app.confirmAction(turn)
  await app.clearSession()
  await app.send('查看客户')
  assert.deepEqual(requests.map(r => r.url), ['/agent/retract'])
  finish({ data: { retracted: true } })
  await revoking
  assert.equal(turn.cancelled, true)
  assert.equal(app.sessionBusy.value, false)
})

test('a cross-window clear during a partial batch clears context while preserving successful receipts', async () => {
  let app
  app = page(async (url) => {
    if (url === '/agent/prepare-create') return { data: {
      session_id: 'flow-test', session_version: 2, context: app.agentContext.value,
      items: ['C000001', 'C000002'].map(customer_id => ({ customer_id,
        assignee: 'tester', confirmation_token: customer_id })), failed: [] } }
    if (url === '/agent/confirm-batch') return { data: {
      session_id: 'flow-test', session_version: 0, expired: true,
      context: { customers: [], orders: [] }, succeeded: 1,
      created: [{ id: 57, customer_id: 'C000001' }],
      failed: [{ customer_id: 'C000002', reason: '会话已清空' }] } }
    throw new Error(`Unexpected endpoint ${url}`)
  })
  app.turns.value.push({ answer: { entities: [] } })
  const turn = app.turns.value[0]
  await app.openBatch(turn, { targets: ['C000001', 'C000002'] })
  await app.submitBatch(turn)
  assert.equal(app.sessionVersion.value, 0)
  assert.deepEqual(app.agentContext.value, { customers: [], orders: [] })
  assert.equal(turn.batch.result.succeeded, 1)
  assert.equal(turn.batch.result.created[0].id, 57)
  assert.equal(turn.batch.result.failed_count, 1)
})


test('Chinese IME confirmation and Shift+Enter do not accidentally send a question', async () => {
  let requests = 0, prevented = 0
  const app = page(async () => { requests++; return { data: {} } })
  app.question.value = '核对客户'
  app.onComposerKeydown({ key: 'Enter', isComposing: true, preventDefault() { prevented++ } })
  app.onComposerKeydown({ key: 'Enter', shiftKey: true, preventDefault() { prevented++ } })
  assert.equal(requests, 0)
  assert.equal(prevented, 0)
  assert.equal(app.question.value, '核对客户')
  assert.equal(app.turns.value.length, 0)
})
