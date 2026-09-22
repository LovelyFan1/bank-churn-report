/**
 * 验收脚本：经 Cloudflare 隧道访问公网地址，验证完整登录链路。
 *
 * ⚠ 纪律（本项目已两次因测试脚本损坏真实数据）：
 *   1) 本脚本**只读**：不调用任何写接口（不建单/不改单/不删单）
 *   2) 不 mock 任何响应 —— 走真实后端，才能验出真问题
 *   3) 只断言，不清理、不播种
 */
import { chromium } from 'playwright-core'
import crypto from 'node:crypto'


// ⚠ 变量名不要叫 URL —— 会遮蔽全局 URL 构造函数，
//   后续 new URL(...) 报 "URL is not a constructor"（已踩过）。
const TUNNEL_URL = process.argv[2] || 'https://blessed-ghz-fast-francis.trycloudflare.com'
const EXE = process.env.CHROME_PATH

function totpNow(secret) {
  // 与后端 auth_service.totp_now 对齐（SHA1 / 6 位 / 30 秒步长）
  const B32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  let bits = ''
  for (const c of secret.toUpperCase().replace(/=+$/, '')) {
    const i = B32.indexOf(c)
    if (i < 0) continue
    bits += i.toString(2).padStart(5, '0')
  }
  const bytes = []
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2))
  const key = Buffer.from(bytes)
  const counter = Math.floor(Date.now() / 30000)
  const buf = Buffer.alloc(8)
  buf.writeUInt32BE(Math.floor(counter / 2 ** 32), 0)
  buf.writeUInt32BE(counter >>> 0, 4)
  const h = crypto.createHmac('sha1', key).update(buf).digest()
  const off = h[h.length - 1] & 0x0f
  const code = ((h[off] & 0x7f) << 24 | h[off + 1] << 16 | h[off + 2] << 8 | h[off + 3]) % 1000000
  return String(code).padStart(6, '0')
}

const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ ignoreHTTPSErrors: true })
const page = await ctx.newPage()

const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push('PAGEERROR ' + e.message))

// 记录所有写请求（用于证明脚本没写数据）
const writes = []
page.on('request', (r) => {
  const m = r.method().toUpperCase()
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m)) writes.push(`${m} ${new URL(r.url()).pathname}`)
})

try {
  console.log(`\n=== 经公网访问 ${TUNNEL_URL} ===`)
  await page.goto(TUNNEL_URL, { waitUntil: 'networkidle', timeout: 60000 })

  // 1) 登录页渲染
  ok(page.url().includes('/login'), `重定向到登录页 (${page.url()})`)
  const title = await page.title()
  ok(!!title, `页面标题: ${title}`)

  // 2) 演示区应出现（AUTH_DEMO_SHOW_TOTP=true）
  await page.waitForTimeout(1200)

  // 3) 用 liming（无需 TOTP）登录
  await page.locator('input[autocomplete="username"]').fill('liming')
  await page.locator('input[autocomplete="current-password"]').fill('Bank@2026')
  await page.locator('button[type="submit"]').click()
  await page.waitForTimeout(4000)
  ok(!page.url().includes('/login'), `登录后离开登录页 (${page.url()})`)

  // 4) 关键：经公网加载业务数据
  const resp = await page.evaluate(async () => {
    const t = localStorage.getItem('auth.token.v1')
    const r = await fetch('/api/customers?limit=5', { headers: { Authorization: 'Bearer ' + t } })
    const j = await r.json()
    return { status: r.status, n: (j.items || j.data || j.customers || []).length, keys: Object.keys(j).slice(0, 8) }
  })
  ok(resp.status === 200, `/api/customers 经公网可访问 (HTTP ${resp.status})`)
  ok(resp.n > 0, `返回客户数据 ${resp.n} 条`)

  // 5) 侧边栏导航（⚠ 导航项是 button/div，不是 <a>，别按链接数断言 —— 已踩过）
  const navText = await page.evaluate(() => {
    const el = document.querySelector('aside')
    return el ? (el.innerText || '').replace(/\s+/g, ' ').trim() : ''
  })
  const NAV_WORDS = ['工作台', '客户名单', '挽留工单', '干预策略']
  const hit = NAV_WORDS.filter((w) => navText.includes(w))
  ok(hit.length >= 3, `侧边栏渲染正常（命中 ${hit.length}/4：${hit.join('、')}）`)

  // 5b) 工作台业务内容确实渲染出来了（不是白屏）
  const dashText = await page.evaluate(() => document.body.innerText || '')
  ok(/在管客户/.test(dashText) && /\d{2,}/.test(dashText), '工作台业务数据已渲染')

  // 6) 控制台无错误
  const realErrors = errors.filter((e) => !/favicon|404 \(Not Found\)/i.test(e))
  ok(realErrors.length === 0, `控制台错误 ${realErrors.length} 条` + (realErrors.length ? ' :: ' + realErrors.slice(0, 3).join(' | ') : ''))

  // 7) 断言脚本未写数据
  const bizWrites = writes.filter((w) => !/\/api\/auth\/(login|logout|demo)/.test(w))
  ok(bizWrites.length === 0, `业务写请求 ${bizWrites.length} 个（应为 0）` + (bizWrites.length ? ' :: ' + bizWrites.join(', ') : ''))
} catch (e) {
  ok(false, '异常: ' + e.message)
} finally {
  const pass = results.filter((r) => r[0]).length
  console.log(`\n=== 结果 ${pass}/${results.length} 通过 ===`)
  console.log('写请求明细: ' + (writes.join(', ') || '无'))
  await browser.close()
  process.exit(pass === results.length ? 0 : 1)
}
