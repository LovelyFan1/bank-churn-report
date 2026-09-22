/**
 * 共享的只读护栏 —— 供所有浏览器 E2E 脚本复用。
 *
 * ⚠ 为什么需要这个文件（实测安全事故）：
 *   我原先在各脚本里分别写 page.route(...) 拦截写请求，其中 diag118
 *   注册了多条（一条拦 /agent/confirm、一条拦 /agent/confirm-batch）。
 *   实测发现 **confirm 请求穿透到了后端**，导致用户手工创建的工单
 *   #63 被真实删除（后端日志：172.18.0.4 POST /api/agent/confirm）。
 *
 *   根因：Playwright 的 route 按注册顺序匹配，多条叠加时具体 pattern
 *   未必优先，可能被后续更宽泛的规则覆盖。
 *
 *   正确做法（本文件）：**只注册一条**覆盖全部 /api 的兜底路由，
 *   在**同一条 handler 内部**按 URL + method 分发。
 *   这样不存在覆盖问题，且非 GET 一律拦截 —— 默认拒绝，而非默认放行。
 *
 * 用法：
 *   import { installReadOnlyGuard } from './_guard.mjs'
 *   const guard = await installReadOnlyGuard(page)
 *   // ... 测试 ...
 *   console.log(guard.report())   // 看拦截了什么
 */

/**
 * 给测试一个干净的起点：清掉 localStorage 会话缓存后重新加载。
 *
 * ⚠ 为什么单独抽出来（实测问题）：第十六节给助手页加了 localStorage
 *   持久化后，**上一个测试留下的对话会一起恢复**，导致
 *   `.turn.agent').last()` / `.nth(1)` 取到的是上一轮的旧内容 ——
 *   多个脚本互相污染，误报频发。
 *   所有要断言"本轮回答"的脚本都应先调用本函数。
 */
export async function resetAgentSession(page, base = 'http://localhost:5173/assistant') {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1800)
  await page.evaluate(() => {
    try {
      localStorage.removeItem('agent.session.v1')
    } catch (_) { /* 忽略 */ }
  })
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(2200)
}

export async function installReadOnlyGuard(page, opts = {}) {
  const {
    // 允许放行的写请求。
    //
    // ⚠ 默认必须包含 /api/agent/ask —— 它是 POST，但**只读**：
    //   本系统的架构保证 /ask 永不写库（写操作走独立的 /confirm）。
    //   实测踩坑：不加这条豁免时，护栏把主查询也拦掉并回一个空假响应，
    //   页面于是显示"口径: 未知"，测试全线误报 ——
    //   看起来像产品坏了，实际是护栏把正常请求也拒了。
    allow = ['/api/agent/ask'],
    // 假响应构造器：按 url 返回 {status, body}
    fake = {},
  } = opts

  const blocked = []   // 被拦截的写请求（URL 字符串，兼容既有断言）
  const blockedDetail = []  // 被拦截的写请求明细（含 postData，供断言 payload）
  // 被**显式 allow** 放行的写请求（如只读的 POST /api/agent/ask）。
  // ⚠ 曾命名为 leaked 并在报告里显示"泄漏 N" —— 那是**误导性标签**：
  //   这些是设计上允许的，不是泄漏。已改名 allowedWrites。
  const allowedWrites = []
  const gets = []      // 放行的读请求数
  // 便捷视图：只含批量建单请求的解析后 payload。
  // ⚠ 是**活数组**（handler 里 push），不是注册时的快照 ——
  //   若在注册后一次性 map，会永远得到空数组。
  const batchPayloads = []

  await page.route('**/api/**', async route => {
    const req = route.request()
    const url = req.url()
    const method = req.method().toUpperCase()
    const path = url.replace(/^https?:\/\/[^/]+/, '')

    if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') {
      gets.push(path)
      return route.continue()
    }

    // 写请求
    const allowed = allow.some(a => path.includes(a))
    if (allowed) {
      allowedWrites.push(`${method} ${path}`)
      return route.continue()
    }

    blocked.push(`${method} ${path}`)
    let body_text = null
    try { body_text = req.postData() } catch (_) { /* 忽略 */ }
    blockedDetail.push({ method, path, url, body: body_text })
    if (path.includes('/confirm-batch')) {
      try { batchPayloads.push(JSON.parse(body_text || '{}')) }
      catch (_) { batchPayloads.push({}) }
    }

    // 找匹配的假响应；没有就用通用成功
    let status = 200
    let body = {}
    for (const [key, spec] of Object.entries(fake)) {
      if (path.includes(key)) {
        status = spec.status ?? 200
        body = spec.body ?? {}
        break
      }
    }
    if (path.includes('/agent/confirm')) {
      status = status ?? 200
      body = body && Object.keys(body).length ? body
        : { created: false, deleted: true, order_id: 999999,
            order: { id: 999999, status: 'pending' } }
    }
    if (path.includes('/work-orders') && method !== 'GET') {
      body = body && Object.keys(body).length ? body
        : { id: 999999, status: 'pending', customer_id: 'TEST' }
      status = method === 'POST' ? 201 : 200
    }
    if (path.includes('/confirm-batch')) {
      body = body && Object.keys(body).length ? body
        : { requested: 0, succeeded: 0, failed_count: 0, skipped_count: 0,
            created: [], failed: [], skipped: [] }
    }

    return route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    })
  })

  return {
    blocked,
    blockedDetail,
    batchPayloads,
    allowedWrites,
    gets,
    /** 有写请求穿透到后端即为严重事故 */
    isSafe: () => allowedWrites.every(w => w.includes('/api/agent/ask')),
    report() {
      const unexpected = allowedWrites.filter(w => !w.includes('/api/agent/ask'))
      return {
        blockedWrites: blocked.length,
        allowedWrites: allowedWrites.length,
        allowedDetail: allowedWrites,
        unexpectedAllowed: unexpected,
        gets: gets.length,
        safe: unexpected.length === 0,
      }
    },
  }
}
