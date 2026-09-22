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

import { createHmac } from 'node:crypto'

/**
 * 登录辅助 —— 引入 4A 登录系统后，**所有** E2E 脚本都必须先登录。
 *
 * ⚠ 为什么必须加这个（实测）：
 *   鉴权改成了**默认拒绝**（见 backend/app/middleware/auth.py），
 *   未登录访问任何 /api/* 都返回 401。前端守卫随即把页面重定向到
 *   /login —— 于是既有脚本全部卡在 `locator('.composer')` 超时上，
 *   报错信息完全看不出"其实是没登录"（表现为找不到元素）。
 *
 * ⚠ 为什么直接塞 localStorage 而不是走登录表单：
 *   绝大多数脚本要测的是**业务页面**，登录只是前置条件。
 *   走表单会让每个脚本多花 2~4 秒，且一旦登录页改版就要改所有脚本。
 *   需要验证登录流程本身的脚本（diag133）才走真实表单。
 *
 * ⚠ 令牌通过真实接口取得（不是伪造），故仍然是端到端有效的。
 */
export async function loginAs(page, username = 'liming',
                              password = 'Bank@2025',
                              base = 'http://localhost:5173') {
  // 用页面上下文发请求，确保同源且走真实的鉴权链路
  const resp = await page.request.post(`${base}/api/auth/login`, {
    data: { username, password },
  })
  if (!resp.ok()) {
    throw new Error(`登录失败 ${resp.status()}: ${await resp.text()}`)
  }
  const data = await resp.json()

  let token = data.token
  let user = data.user

  // 管理员需要第二步动态口令（TOTP）。
  // ⚠ 这里用与后端相同的算法**当场算出**当前口令，而不是硬编码 ——
  //   TOTP 每 30 秒变一次，硬编码必然失效。
  if (data.need_totp && data.ticket) {
    const secret = process.env.TEST_TOTP_SECRET
    if (!secret) {
      throw new Error(
        '该账号需要动态口令，但未提供 TEST_TOTP_SECRET 环境变量。\n' +
        '密钥见后端启动日志 "[preseed] TOTP zhaomin: <secret>"。')
    }
    const code = totpNow(secret)
    const r2 = await page.request.post(`${base}/api/auth/login/totp`, {
      data: { ticket: data.ticket, code },
    })
    if (!r2.ok()) throw new Error(`动态口令失败 ${r2.status()}: ${await r2.text()}`)
    const d2 = await r2.json()
    token = d2.token
    user = d2.user
  }

  await page.goto(base, { waitUntil: 'domcontentloaded' })
  await page.evaluate(([t, u]) => {
    localStorage.setItem('auth.token.v1', t)
    localStorage.setItem('auth.user.v1', JSON.stringify(u))
  }, [token, user])
  return user
}

/** 与后端 `_hotp` 同算法（RFC 6238, SHA1, 6 位）—— 避免引入依赖。 */
export function totpNow(secretB32, step = 30) {
  const A = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  const pad = '='.repeat((8 - (secretB32.length % 8)) % 8)
  const b32 = secretB32.toUpperCase() + pad
  let bits = ''
  for (const ch of b32) {
    if (ch === '=') break
    const idx = A.indexOf(ch)
    if (idx < 0) throw new Error(`非法 base32 字符: ${ch}`)
    bits += idx.toString(2).padStart(5, '0')
  }
  const bytes = []
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2))
  }
  const key = Buffer.from(bytes)
  const counter = Math.floor(Date.now() / 1000 / step)
  const msg = Buffer.alloc(8)
  msg.writeBigUInt64BE(BigInt(counter))
  const h = createHmac('sha1', key).update(msg).digest()
  const off = h[h.length - 1] & 0x0f
  const num = ((h[off] & 0x7f) << 24) | (h[off + 1] << 16) |
              (h[off + 2] << 8) | h[off + 3]
  return String(num % 1000000).padStart(6, '0')
}

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
    //
    // ⚠ 认证类接口同样必须放行（引入登录系统后新增）：
    //   它们是 POST，但**不写业务数据**，只操作会话与用户自身的状态。
    //   实测踩坑：登录请求被拦 → 前端拿不到令牌 → 页面停在登录页 →
    //   后续所有断言以 `locator timeout` 失败，报错完全看不出是护栏所致。
    //   /change-password 会改 users 表，故**不放行**（它不该出现在只读测试里）。
    allow = [
      '/api/agent/ask',
      '/api/auth/login',
      '/api/auth/login/totp',
      '/api/auth/logout',
      // 演示取码：POST 但只读（用临时票据换当前动态口令）——
      // 它不写任何业务数据。⚠ 实测教训：不加这条时护栏返回**空对象 {}**，
      // 而前端 `v-if="demo"` 判的是 truthy —— 于是界面渲染出空的演示区，
      // 6 项断言莫名失败（表现为"口令为空"），完全看不出是护栏所致。
      '/api/auth/demo/totp',
    ],
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

  // ⚠ 判据必须用**动态的 allow 清单**，不能硬编码 '/api/agent/ask'。
  //   引入登录后放行清单增加了认证接口；若断言仍只认 ask，
  //   正常的登录请求会被报成"预期外的写请求"（假告警）。
  const isAllowed = (w) => allow.some(a => w.includes(a))

  return {
    blocked,
    blockedDetail,
    batchPayloads,
    allowedWrites,
    gets,
    /** 有写请求穿透到后端即为严重事故 */
    isSafe: () => allowedWrites.every(isAllowed),
    report() {
      const unexpected = allowedWrites.filter(w => !isAllowed(w))
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
