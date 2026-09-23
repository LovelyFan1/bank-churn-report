/**
 * 隧道可用性验证 —— 从公网地址真实走一遍登录 + 页面。
 *
 * ⚠ 只读：不点建单/改单/删除。断言业务写请求（POST /api/work-orders*、
 *   /api/agent/confirm*）为 0。
 *
 * 用法： node verify-tunnel-live.mjs https://xxx.trycloudflare.com
 * 运行目录： %TEMP%\pw-probe
 */
import { chromium } from 'playwright-core'

const URL_ARG = process.argv[2]
if (!URL_ARG) { console.error('usage: node verify-tunnel-live.mjs <url>'); process.exit(2) }

const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1500, height: 980 } })
const page = await ctx.newPage()
const errs = []
page.on('pageerror', (e) => errs.push('PAGEERROR ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })

// 业务写请求留痕 —— 只读验证的核心断言
const bizWrites = []
page.on('request', (r) => {
  const m = r.method().toUpperCase()
  const p = new URL(r.url()).pathname
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m) &&
      !p.startsWith('/api/auth/')) bizWrites.push(m + ' ' + p)
})

console.log('=== 目标:', URL_ARG, '===')
await page.goto(URL_ARG, { waitUntil: 'networkidle', timeout: 90000 })
await page.waitForTimeout(2000)

const title = await page.title()
ok(!!title, `页面标题可读取: ${title}`)
ok(await page.locator('input[autocomplete="username"]').count() > 0, '登录表单已渲染（Vue 应用已挂载）')

// 登录管理员
await page.locator('input[autocomplete="username"]').fill('zhaomin')
await page.locator('input[autocomplete="current-password"]').fill('Bank@2026')
await page.locator('button[type="submit"]').click()
await page.waitForTimeout(3000)

const totp = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]')
if (await totp.count()) {
  const fb = page.locator('button:has-text("填入"), button:has-text("使用"), button:has-text("获取")')
  if (await fb.count()) { await fb.first().click(); await page.waitForTimeout(800) }
  let c = await totp.first().inputValue().catch(() => '')
  if (!/^\d{4,6}$/.test(c || '')) {
    const m = (await page.evaluate(() => document.body.innerText)).match(/\b(\d{6})\b/)
    if (m) await totp.first().fill(m[1])
  }
  await page.locator('button[type="submit"]').click()
  await page.waitForTimeout(3000)
}

const loggedIn = !page.url().includes('/login')
ok(loggedIn, `登录成功（双因素），当前路径 ${new URL(page.url()).pathname}`)

// 侧栏导航 — 确认权限数据经隧道完整送达
const nav = await page.evaluate(() => (document.querySelector('aside')?.innerText || ''))
ok(nav.includes('工作台'), '侧栏含「工作台」')
ok(nav.includes('工单'), '侧栏含「工单」')

// 工作台真实数据（证明后端经隧道可读）
await page.goto(URL_ARG + '/dashboard', { waitUntil: 'networkidle', timeout: 60000 })
await page.waitForTimeout(5000)
const dash = await page.evaluate(() => document.body.innerText)
ok(/\d/.test(dash) && dash.length > 200, `工作台有内容（${dash.length} 字符）`)
ok(!/Network Error|无法连接|请求失败/i.test(dash), '无网络错误提示')

// 关键：业务写请求必须为 0
ok(bizWrites.length === 0, `业务写请求为 0（实测 ${bizWrites.length} 条${bizWrites.length ? ': ' + bizWrites.join(', ') : ''}）`)

const realErrs = errs.filter(e => !/favicon|404 \(Not Found\)/i.test(e))
ok(realErrs.length === 0, `无 JS 报错（${realErrs.length} 条${realErrs.length ? ': ' + realErrs.slice(0, 2).join(' | ') : ''}）`)

const pass = results.filter(r => r[0]).length
console.log(`\n结果: ${pass}/${results.length} 通过`)
await browser.close()
process.exit(pass === results.length ? 0 : 1)
