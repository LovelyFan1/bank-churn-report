import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import { ref, reactive, computed } from 'vue'

const { descriptor } = parse(readFileSync(new URL('../src/components/PrepareOrderDialog.vue', import.meta.url), 'utf8'))
const setup = descriptor.scriptSetup.content.replace(/^import .+$/gm, '')
const instantiate = new Function('deps', `const { ref, reactive, computed, watch, onBeforeUnmount,
  defineProps, defineEmits, api, useAuthStore, assignees, canAssign, loadAssignees, defaultAssignee } = deps;
  ${setup}
  return { load, submit, form, customer, created, error, loading, submitting, canSubmit, blocked };`)
const sample = { customer_id: 'C034525', surname: '陈明', risk_level: 'HIGH', exited: 0, has_active_order: false,
  balance: 100000, probability: .6, value_tier: 'HIGH', expected_value: 60000, channel: 'relationship', action: '核实近期服务体验' }
function page({ post = async () => ({ data: { id: 125, customer_id: sample.customer_id } }), getCustomer = async () => sample } = {}) {
  const props = reactive({ open: true, customerId: sample.customer_id }), events = []
  const app = instantiate({ ref, reactive, computed, watch() {}, onBeforeUnmount() {}, defineProps: () => props,
    defineEmits: () => (...args) => events.push(args), api: { post, async get(url) {
      if (url.endsWith('/suggested-note')) return { data: { note: '核实近期服务体验' } }
      if (url === '/model/risk-info') return { data: { model: 'LightGBM', thresholds: { high: .6 } } }
      return { data: await getCustomer(url) }
    } }, useAuthStore: () => ({ can: () => true, user: { username: 'tester' } }), assignees: ref([]), canAssign: ref(true),
    loadAssignees: async () => [], defaultAssignee: () => 'tester' })
  return { app, props, events }
}

test('preparation reads current customer data; creation requires explicit submit and cannot double-submit', async () => {
  const requests = []; let finish
  const { app, events } = page({ post: (url, body) => { requests.push({ url, body }); return new Promise(resolve => { finish = resolve }) } })
  await app.load()
  assert.equal(requests.length, 0)
  const first = app.submit()
  await app.submit()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].body.assignee, 'tester')
  assert.equal(requests[0].body.channel, 'relationship')
  assert.equal(requests[0].body.expected_value_snapshot, 60000)
  finish({ data: { id: 125, customer_id: sample.customer_id } })
  await first
  await app.submit()
  assert.equal(requests.length, 1)
  assert.equal(events[0][0], 'created')
  assert.equal(app.created.value.id, 125)
})

test('active orders, historical churn and unexplained channel changes cannot be submitted', async () => {
  let count = 0
  const { app } = page({ post: async () => { count++; return { data: {} } } })
  await app.load()
  app.customer.value.has_active_order = true
  await app.submit()
  app.customer.value.has_active_order = false; app.customer.value.exited = 1
  await app.submit()
  app.customer.value.exited = 0; app.form.channel = 'outbound'
  await app.submit()
  assert.equal(count, 0)
  app.form.override_reason = '客户明确要求电话联系'
  await app.submit()
  assert.equal(count, 1)
})

test('a failed create preserves the edited note and allows a deliberate retry', async () => {
  const { app } = page({ post: async () => { throw { response: { data: { detail: '该客户已有在途工单' } } } } })
  await app.load()
  app.form.note = '客户经理补充的备注'
  await app.submit()
  assert.equal(app.error.value, '该客户已有在途工单')
  assert.equal(app.form.note, '客户经理补充的备注')
  assert.equal(app.submitting.value, false)
  assert.equal(app.created.value, null)
})

test('late customer responses cannot replace the newly selected customer', async () => {
  let finishOld
  const { app, props } = page({ getCustomer: url => url.endsWith('C034525') ? new Promise(resolve => { finishOld = resolve }) : Promise.resolve({ ...sample, customer_id: 'C018629', surname: '王林' }) })
  const old = app.load()
  props.customerId = 'C018629'
  await app.load()
  finishOld({ ...sample })
  await old
  assert.equal(app.customer.value.customer_id, 'C018629')
  assert.equal(app.customer.value.surname, '王林')
})
