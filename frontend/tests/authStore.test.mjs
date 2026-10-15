import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { createPinia, setActivePinia } from 'pinia'

const storage = new Map()
const previousStorage = globalThis.localStorage
globalThis.localStorage = {
  getItem: key => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: key => storage.delete(key),
}
const server = await createServer({ root: fileURLToPath(new URL('..', import.meta.url)),
  configFile: false, appType: 'custom', server: { middlewareMode: true, hmr: false, watch: null },
})
const { useAuthStore } = await server.ssrLoadModule('/src/stores/auth.js')
const { default: api, setUnauthorizedHandler } = await server.ssrLoadModule('/src/api/index.js')
const { KEY_TOKEN, KEY_USER } = await server.ssrLoadModule('/src/utils/userStorage.js')
const { pollTask } = await server.ssrLoadModule('/src/api/taskPoller.js')
const assigneeModule = await server.ssrLoadModule('/src/utils/assignees.js')
const originalGet = api.get
const originalPost = api.post
after(async () => {
  api.get = originalGet
  api.post = originalPost
  setUnauthorizedHandler(null)
  globalThis.localStorage = previousStorage
  await server.close()
})
function fixture() {
  storage.clear()
  storage.set(KEY_TOKEN, 'fixture-token')
  storage.set(KEY_USER, JSON.stringify({ username: 'fixture' }))
  setActivePinia(createPinia())
  return useAuthStore()
}

test('登录与刷新采用服务端策略和剩余期限', async () => {
  const auth = fixture()
  api.post = async () => ({ data: { token: 'new-fixture-token', user: { username: 'fixture' },
    idle_timeout: 180, idle_remaining: 180 } })
  await auth.login('fixture', 'fixture-password')
  assert.equal(auth.idleTimeout, 180)
  api.get = async () => ({ data: { user: { username: 'fixture' }, idle_timeout: 180, idle_remaining: 41 } })
  await auth.fetchMe()
  assert.equal(auth.idleRemaining, 41)
})

test('退出后迟到的身份响应不能恢复用户或本地身份缓存', async () => {
  const auth = fixture()
  let resolve
  api.get = () => new Promise(done => { resolve = done })
  const response = auth.fetchMe()
  auth.clear()
  resolve({ data: { user: { username: 'fixture' }, idle_timeout: 180, idle_remaining: 180 } })
  await assert.rejects(response, { code: 'ERR_CANCELED' })
  assert.equal(auth.user, null)
  assert.equal(storage.get(KEY_USER), undefined)
  assert.equal(storage.get(KEY_TOKEN), undefined)
})

test('退出后迟到的活动响应不能重置策略', async () => {
  const auth = fixture()
  let resolve
  api.post = () => new Promise(done => { resolve = done })
  const response = auth.reportActivity()
  auth.clear()
  resolve({ data: { idle_timeout: 180, idle_remaining: 180 } })
  await assert.rejects(response, { code: 'ERR_CANCELED' })
  assert.equal(auth.idleTimeout, 0)
  assert.equal(auth.idleRemaining, 0)
})

test('旧账号请求的401不会清理当前新账号，当前令牌401仍通知退出', async () => {
  storage.set(KEY_TOKEN, 'current-token')
  let calls = 0
  setUnauthorizedHandler(() => { calls += 1 })
  const handler = api.interceptors.response.handlers[0].rejected
  for (const [sent, expected] of [['old-token', 0], ['current-token', 1]]) {
    const error = { config: { headers: { Authorization: 'Bearer ' + sent } },
      response: { status: 401, data: { detail: 'expired' } } }
    await assert.rejects(handler(error))
    assert.equal(calls, expected)
  }
})

test('任务终态停止轮询，业务失败可见', async () => {
  for (const data of [{ status: 'SUCCESS', result: { status: 'completed', value: 1 } },
                     { status: 'FAILURE', result: { error: 'fixture failure' } }]) {
    let calls = 0
    api.get = async () => { calls += 1; return { data } }
    if (data.status === 'SUCCESS') assert.equal((await pollTask('fixture')).value, 1)
    else await assert.rejects(pollTask('fixture'), /fixture failure/)
    assert.equal(calls, 1)
  }
})

test('卸载在等待间隔内立即停止任务轮询', async () => {
  const controller = new AbortController()
  let calls = 0
  api.get = async () => { calls += 1; return { data: { status: 'PENDING' } } }
  const result = pollTask('fixture', { signal: controller.signal, interval: 60000 })
  const rejection = assert.rejects(result, { code: 'ERR_CANCELED' })
  await new Promise(resolve => setTimeout(resolve, 0))
  controller.abort()
  await rejection
  assert.equal(calls, 1)
})

test('仅有幂等键的模型任务POST可重试并保留原键', async () => {
  const handler = api.interceptors.response.handlers[0].rejected
  const observed = []
  const config = { method: 'post', url: '/model/train', headers: { 'Idempotency-Key': 'fixture-retry-key' },
    adapter: async config => {
      observed.push(config.headers.get('Idempotency-Key'))
      return { data: { task_id: 'fixture' }, status: 200, statusText: 'OK', headers: {}, config }
    } }
  assert.equal((await handler({ config, message: 'fixture connection lost' })).data.task_id, 'fixture')
  assert.deepEqual(observed, ['fixture-retry-key'])
  await assert.rejects(handler({ config: { ...config, url: '/work-orders' }, message: 'fixture connection lost' }))
  assert.equal(observed.length, 1)
})

test('换账号后迟到的负责人目录不能恢复旧账号状态，同名显示工号', async () => {
  assigneeModule.resetAssignees()
  let resolve
  api.get = () => new Promise(done => { resolve = done })
  const loading = assigneeModule.loadAssignees()
  assigneeModule.resetAssignees()
  resolve({ data: { items: [{ username:'old', display_name:'旧账号', department:'A' }], can_assign:true } })
  await loading
  assert.deepEqual(assigneeModule.assignees.value, [])
  assert.equal(assigneeModule.canAssign.value, false)
  assigneeModule.assignees.value = [{ username:'first', display_name:'同名', department:'A' }, { username:'second', display_name:'同名', department:'B' }]
  assert.match(assigneeModule.assigneeName('first'), /first·A/)
  assert.equal(assigneeModule.displayAssignee('同名').legacy, true)
})
