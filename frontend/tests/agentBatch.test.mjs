import test from 'node:test'
import assert from 'node:assert/strict'
import { batchPlan } from '../src/utils/agentBatch.js'

test('each explicit batch preserves all deduplicated targets without exceeding twenty', () => {
  for (const size of [20, 21, 25, 50]) {
    const ids = Array.from({ length: size }, (_, i) => `C${String(i + 1).padStart(6, '0')}`)
    let plan = batchPlan([...ids, ids[0].toLowerCase()])
    const observed = []
    while (plan.current.length) {
      assert.ok(plan.current.length <= 20)
      observed.push(...plan.current)
      plan = batchPlan(plan.remaining)
    }
    assert.deepEqual(observed, ids)
  }
})
