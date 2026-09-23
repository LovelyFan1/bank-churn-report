/**
 * 端到端验收两个 bug 的修复（浏览器）：
 *   Bug1: 审计页时间 = 北京时间
 *   Bug2: "这三位"建单 → 批量面板 + 负责人下拉可选且已预填
 *
 * ⚠ 只读：不点"确认建单"。断言业务写请求为 0。
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'https://blessed-ghz-fast-francis.trycloudflare.com'
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1500, height: 980 } })
const page = await ctx.newPage()
const errs = []
page.on('pageerror', (e) => errs.push('PAGEERROR ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })
const writes = []
page.on('request', (r) => {
  const m = r.method().toUpperCase()
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m)) writes.push(m + ' ' + new URL(r.url()).pathname)
})

// 登录管理员（zhaomin 有 audit:view + agent:use）
await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 })
await page.locator('input[autocomplete="username"]').fill('zhaomin')
await page.locator('input[autocomplete="current-password"]').fill('Bank@2026')
await page.locator('button[type="submit"]').click()
await page.waitForTimeout(2500)
const totp = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]')
if (await totp.count()) {
  const fb = page.locator('button:has-text("填入"), button:has-text("使用")')
  if (await fb.count()) { await fb.first().click(); await page.waitForTimeout(700) }
  let c = await totp.first().inputValue().catch(() => '')
  if (!/^\d{4,6}$/.test(c || '')) {
    const m = (await page.evaluate(() => document.body.innerText)).match(/\b(\d{6})\b/)
    if (m) await totp.first().fill(m[1])
  }
  await page.locator('button[type="submit"]').click()
}
await page.waitForFunction(() => !location.pathname.includes('/login'), null, { timeout: 30000 }).catch(() => {})
await page.waitForTimeout(3000)
console.log('logged in as zhaomin ->', page.url())

// ══════════════════════════════════════════════════════
console.log('\n########## Bug1: 审计页时间 ##########')
// ══════════════════════════════════════════════════════
await page.goto(BASE + '/audit', { waitUntil: 'networkidle', timeout: 60000 })
await page.waitForTimeout(6000)

const audit = await page.evaluate(() => {
  const rows = Array.from(document.querySelectorAll('tbody tr'))
  const first = rows[0]
  const tds = first ? Array.from(first.querySelectorAll('td')) : []
  return {
    count: rows.length,
    firstTime: tds[0]?.innerText?.trim() || '',
    firstWho: tds[1]?.innerText?.trim() || '',
    firstAction: tds[3]?.innerText?.trim() || '',
    browserNow: new Date().toString(),
    browserTz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  }
})
console.log('  browser now :', audit.browserNow)
console.log('  browser tz  :', audit.browserTz)
console.log('  rows        :', audit.count)
console.log('  first row   :', audit.firstTime, '|', audit.firstWho, '|', audit.firstAction)

ok(audit.count > 0, 'audit table rendered with rows')
ok(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(audit.firstTime),
   'time has expected format -> ' + audit.firstTime)

// 与北京时间对照：审计最新一条应在最近 N 分钟内
const timeCheck = await page.evaluate((shown) => {
  // 把显示的本地时间串解析成本地时刻，与"现在"比
  const m = shown.match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/)
  if (!m) return { ok: false, diffMin: null }
  const dt = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])
  const diffMin = (Date.now() - dt.getTime()) / 60000
  return { ok: true, diffMin, parsed: dt.toString() }
}, audit.firstTime)

console.log('  parsed as local:', timeCheck.parsed)
console.log('  minutes ago    :', timeCheck.diffMin?.toFixed(1))
ok(timeCheck.ok && timeCheck.diffMin != null && timeCheck.diffMin >= -5 && timeCheck.diffMin < 240,
   '*** latest audit time is within the last few hours (NOT 8h behind) -> ' +
   (timeCheck.diffMin != null ? timeCheck.diffMin.toFixed(1) + ' min ago' : 'n/a'))

// 关键反证：若还是旧实现（UTC 原样显示），差值会 ≈ 480 分钟
ok(!(timeCheck.diffMin > 400 && timeCheck.diffMin < 560),
   '*** NOT showing a ~480min (8h) offset — bug is fixed')

// ══════════════════════════════════════════════════════
console.log('\n########## Bug2: Agent 批量建单面板 ##########')
// ══════════════════════════════════════════════════════
await page.goto(BASE + '/assistant', { waitUntil: 'networkidle', timeout: 60000 })
await page.waitForTimeout(5000)

// 第一步：问"挽回价值最高的3个客户"（建立上下文）
async function ask(q) {
  const box = page.locator('textarea, input[type="text"]').first()
  await box.fill(q)
  const send = page.locator('button:has-text("发送")').first()
  if (await send.count()) await send.click()
  else await box.press('Enter')
  // 等回答出现（最多 90s）
  await page.waitForTimeout(20000)
}

console.log('  asking: 挽回价值最高的3个客户')
await ask('挽回价值最高的3个客户')
await page.waitForTimeout(8000)

let body = await page.evaluate(() => document.body.innerText)
const hasList = /C0\d{5}/.test(body)
console.log('  list rendered:', hasList)
ok(hasList, 'first answer rendered a customer list')

// 第二步：说"这三位"
console.log('  asking: 帮我把这三位建立工单')
await ask('帮我把这三位建立工单')
await page.waitForTimeout(10000)

const panel = await page.evaluate(() => {
  const el = document.querySelector('.batch-panel')
  if (!el) return null
  const sel = el.querySelector('.batch-assignee select')
  const inp = el.querySelector('.batch-assignee input')
  return {
    text: el.innerText.replace(/\s+/g, ' ').slice(0, 400),
    hasSelect: !!sel,
    selectOptions: sel ? sel.options.length : 0,
    selectValue: sel ? sel.value : '',
    selectText: sel ? (sel.options[sel.selectedIndex]?.text || '') : '',
    hasReadonlyInput: !!inp,
    itemsRendered: el.querySelectorAll('.batch-items li').length,
    plainListItems: el.querySelectorAll('.batch-list:not(.batch-items) li').length,
  }
})
console.log('  panel:', panel ? JSON.stringify({
  hasSelect: panel.hasSelect, options: panel.selectOptions,
  value: panel.selectValue, selectText: panel.selectText,
  items: panel.itemsRendered, plain: panel.plainListItems,
}) : '(none)')
if (panel) console.log('  panel text:', panel.text.slice(0, 220))

ok(panel !== null, '*** batch panel appeared for 「这三位」')
if (panel) {
  ok(panel.itemsRendered === 3 || panel.plainListItems === 3,
     '*** panel lists 3 customers (was 1 before) -> items=' +
     panel.itemsRendered + ' plain=' + panel.plainListItems)
  ok(panel.hasSelect, '*** assignee is a SELECT (dropdown), not a readonly input')
  ok(panel.selectOptions > 1, 'assignee dropdown populated (' + panel.selectOptions + ' options)')
  ok(!!panel.selectValue, 'assignee prefilled -> ' + panel.selectText)
  ok(!/（未指定）/.test(panel.text), '*** no longer shows 「（未指定）」')
}

// ⚠ /api/agent/ask 是**只读对话**（POST 只是因为要传问题体），
//   /api/agent/confirm 才是真正写库的。断言要排除 ask，
//   否则会把"提问"误判成"建单"（本断言踩过一次）。
const WRITE_ENDPOINTS = ['/api/agent/confirm', '/api/work-orders',
                         '/api/agent/confirm-batch']
const biz = writes.filter((w) => {
  if (/\/api\/auth\//.test(w)) return false          // 登录登出
  if (/\/api\/agent\/ask$/.test(w)) return false     // 只读对话
  return WRITE_ENDPOINTS.some((e) => w.includes(e))
})
console.log('\n  all POST/PUT/DELETE:', JSON.stringify(writes))
console.log('  real business writes:', JSON.stringify(biz))
ok(biz.length === 0, 'ZERO real write operations (no order created)')

const real = errs.filter((e) => !/favicon|404/i.test(e))
ok(real.length === 0, 'console errors: ' + real.length +
   (real.length ? ' :: ' + real.slice(0, 2).join(' | ') : ''))

const pass = results.filter((r) => r[0]).length
console.log('\n==================================================')
console.log('RESULT: ' + pass + '/' + results.length + ' passed')
console.log('==================================================')
for (const [c, m] of results) if (!c) console.log('  FAILED:', m)
await browser.close()
process.exit(pass === results.length ? 0 : 1)
