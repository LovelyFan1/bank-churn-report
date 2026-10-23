import test from 'node:test'
import assert from 'node:assert/strict'
import { orderLocation, customerReturn, focusedOrderId } from '../src/utils/workflow.js'

test('a work-order receipt keeps the exact authorized order and customer destination', () => {
  assert.deepEqual(orderLocation({ id: 125, customer_id: 'C034525' }), {
    path: '/work-orders', query: { order_id: '125', customer_id: 'C034525' },
  })
  assert.deepEqual(orderLocation({ order_id: 125 }), { path: '/work-orders', query: { order_id: '125' } })
  assert.deepEqual(orderLocation({ id: 'invalid', customer_id: 'anything' }), { path: '/work-orders', query: {} })
})

test('returning from a customer keeps list filters and rejects unrelated destinations', () => {
  const list = '/customers?risk_level=HIGH&has_order=0&page=2'
  assert.equal(customerReturn(list), list)
  assert.equal(customerReturn('/assistant'), '/assistant')
  for (const value of ['https://outside.example', '//outside.example', '/customers-other', '/login', ['/customers'], undefined]) {
    assert.equal(customerReturn(value), '/customers')
  }
})

test('only positive, safe decimal order identifiers enable direct receipt lookup', () => {
  assert.equal(focusedOrderId('125'), 125)
  for (const value of ['-1', '0', '01', '1.5', '1e3', '9007199254740992', ['125'], undefined]) {
    assert.equal(focusedOrderId(value), null)
  }
})
