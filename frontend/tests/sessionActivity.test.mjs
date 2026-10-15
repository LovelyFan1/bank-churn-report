import test from 'node:test'
import assert from 'node:assert/strict'
import { createSessionActivity } from '../src/utils/sessionActivity.js'

function fixture(report = async () => ({ idle_timeout: 120, idle_remaining: 120 })) {
  let time = 0
  let id = 0
  const timers = new Map()
  const state = { reports: 0, expired: 0, warnings: [] }
  const clock = {
    now: () => time,
    setTimeout(fn, delay) { timers.set(++id, { fn, at: time + delay }); return id },
    clearTimeout(key) { timers.delete(key) },
  }
  const control = createSessionActivity({ clock,
    reportActivity: async () => { state.reports += 1; return report() },
    onWarning: value => state.warnings.push(value), onExpired: () => { state.expired += 1 },
  })
  async function advance(ms) {
    const end = time + ms
    while (true) {
      const entry = [...timers.entries()].filter(([, value]) => value.at <= end)
        .sort((a, b) => a[1].at - b[1].at)[0]
      if (!entry) break
      time = entry[1].at
      timers.delete(entry[0])
      entry[1].fn()
      await settle()
    }
    time = end
    await settle()
  }
  return { control, state, timers, advance }
}
async function settle() { for (let i = 0; i < 6; i += 1) await Promise.resolve() }

test('刷新使用服务端剩余时间，空闲不自动上报', async () => {
  const f = fixture()
  f.control.start({ idle_timeout: 120, idle_remaining: 30 })
  await f.advance(29999)
  assert.equal(f.state.expired, 0)
  assert.ok(f.state.warnings.includes(true))
  await f.advance(1)
  assert.equal(f.state.expired, 1)
  assert.equal(f.state.reports, 0)
})

test('高频真实输入合并上报，停止输入后不产生周期续期', async () => {
  const f = fixture()
  f.control.start({ idle_timeout: 120, idle_remaining: 120 })
  f.control.activity()
  await settle()
  await f.advance(5000)
  for (let i = 0; i < 100; i += 1) f.control.activity()
  assert.equal(f.state.reports, 1)
  await f.advance(10000)
  assert.equal(f.state.reports, 2)
  await f.advance(30000)
  assert.equal(f.state.reports, 2)
})

test('成功活动采用新的服务端期限', async () => {
  const f = fixture(async () => ({ idle_timeout: 60, idle_remaining: 60 }))
  f.control.start({ idle_timeout: 60, idle_remaining: 20 })
  await f.advance(10000)
  f.control.activity()
  await settle()
  await f.advance(59999)
  assert.equal(f.state.expired, 0)
  await f.advance(1)
  assert.equal(f.state.expired, 1)
})

test('网络失败不延长期限也不自动重试', async () => {
  const f = fixture(async () => { throw new Error('offline') })
  f.control.start({ idle_timeout: 60, idle_remaining: 60 })
  await f.advance(10000)
  f.control.activity()
  await settle()
  await f.advance(50000)
  assert.equal(f.state.expired, 1)
  assert.equal(f.state.reports, 1)
})

test('401立即退出并清理待上报任务', async () => {
  const f = fixture(async () => { throw { response: { status: 401 } } })
  f.control.start({ idle_timeout: 120, idle_remaining: 120 })
  f.control.activity()
  await settle()
  assert.equal(f.state.expired, 1)
  assert.equal(f.timers.size, 0)
})

test('到期后的迟到响应不能复活会话', async () => {
  let resolve
  const f = fixture(() => new Promise(done => { resolve = done }))
  f.control.start({ idle_timeout: 60, idle_remaining: 2 })
  f.control.activity()
  await settle()
  await f.advance(2000)
  resolve({ idle_timeout: 60, idle_remaining: 60 })
  await settle()
  f.control.activity()
  assert.equal(f.state.expired, 1)
  assert.equal(f.state.reports, 1)
  assert.equal(f.timers.size, 0)
})

test('卸载取消定时任务，迟到响应不再更新警告', async () => {
  let resolve
  const f = fixture(() => new Promise(done => { resolve = done }))
  f.control.start({ idle_timeout: 120, idle_remaining: 120 })
  f.control.activity()
  await settle()
  f.control.stop()
  const count = f.state.warnings.length
  resolve({ idle_timeout: 120, idle_remaining: 120 })
  await settle()
  await f.advance(300000)
  assert.equal(f.state.warnings.length, count)
  assert.equal(f.state.expired, 0)
  assert.equal(f.timers.size, 0)
})

test('继续使用立即上报且不并发重复请求', async () => {
  const f = fixture()
  f.control.start({ idle_timeout: 120, idle_remaining: 120 })
  f.control.activity()
  await settle()
  await f.advance(1000)
  f.control.activity(true)
  f.control.activity(true)
  await settle()
  assert.equal(f.state.reports, 2)
})
