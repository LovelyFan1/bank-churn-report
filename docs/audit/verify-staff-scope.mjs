/**
 * 端到端验收：staff 权限收敛后的界面表现。
 *
 * 断言：
 *   1) staff 侧边栏只剩「工作台」「挽留工单」（系统管理分组整体隐藏）
 *   2) staff 登录落在工作台，且工作台是**脱敏版**（显示 客户 #N、无姓名列）
 *   3) 直连受限网址（/customers 等）被弹回，且**不会卡住/循环**
 *   4) staff 进工单页仍能看到客户姓名（能打电话）
 *   5) manager / viewer 的导航未被破坏（回归）
 *
 * ⚠ 只读：不建单、不改单、不删单；断言业务写请求为 0。
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'https://blessed-ghz-fast-francis.trycloudflare.com'
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

async function loginAs(username) {
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1500, height: 960 } })
  const page = await ctx.newPage()
  const errs = []
  page.on('pageerror', (e) => errs.push('PAGEERROR ' + e.message))
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })
  const writes = []
  page.on('request', (r) => {
    const m = r.method().toUpperCase()
    if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m)) writes.push(m + ' ' + new URL(r.url()).pathname)
  })

  await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 })
  await page.locator('input[autocomplete="username"]').fill(username)
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
  return { page, ctx, errs, writes }
}

const navText = (page) => page.evaluate(() =>
  (document.querySelector('aside')?.innerText || '').replace(/\s+/g, ' ').trim())

// ══════════════════════════════════════════════════════
console.log('########## 1. staff (wangxiaoyun) ##########')
// ══════════════════════════════════════════════════════
{
  const { page, ctx, errs, writes } = await loginAs('wangxiaoyun')
  console.log('  landed on:', page.url())

  const nav = await navText(page)
  console.log('  sidebar:', nav)

  ok(nav.includes('工作台'), 'staff sees 工作台')
  ok(nav.includes('挽留工单'), 'staff sees 挽留工单')
  ok(!nav.includes('客户名单'), '*** staff does NOT see 客户名单')
  ok(!nav.includes('干预策略'), '*** staff does NOT see 干预策略')
  ok(!nav.includes('智能助手'), 'staff does NOT see 智能助手')
  ok(!nav.includes('系统管理'), '*** 系统管理 group header hidden entirely')
  ok(!nav.includes('客群洞察') && !nav.includes('数据洞察') && !nav.includes('模型效果'),
     '*** staff sees no analysis pages')
  ok(!nav.includes('操作审计'), 'staff does not see 操作审计')

  // 工作台是脱敏版
  await page.goto(BASE + '/dashboard', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(7000)
  const dash = await page.evaluate(() => document.body.innerText)
  const hasMaskBanner = /客户专员|仅处理指派给自己/.test(dash)
  const hasAnonName = /客户 #\d+/.test(dash)
  console.log('  masked banner:', hasMaskBanner, '| anon names:', hasAnonName)
  ok(hasMaskBanner, '*** staff dashboard shows the role-specific mask notice')
  ok(hasAnonName || /暂无/.test(dash), '*** staff dashboard shows anonymized rows (客户 #N)')
  ok(!/查看全部/.test(dash), '「查看全部」link hidden for staff (it would 403)')

  // 直连受限页 → 被弹回且不循环
  for (const p of ['/customers', '/intervention', '/eda', '/models', '/clustering']) {
    await page.goto(BASE + p, { waitUntil: 'networkidle', timeout: 60000 })
    await page.waitForTimeout(2200)
    const u = page.url()
    const landed = u.replace(BASE, '')
    const bodyLen = (await page.evaluate(() => document.body.innerText)).length
    console.log(`  ${p.padEnd(14)} -> ${landed} (${bodyLen} chars)`)
    ok(!landed.startsWith(p), `*** ${p} bounced for staff`)
    ok(bodyLen > 200, `${p} did not land on a blank page (no redirect loop)`)
  }

  // 工单页仍能看到姓名
  await page.goto(BASE + '/work-orders', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(7000)
  const wo = await page.evaluate(() => document.body.innerText)
  ok(/我的工单/.test(wo), 'staff work orders titled 「我的工单」')
  ok(/王晓芸/.test(wo), 'staff own name shown')
  const anonInOrders = /客户 #\d+/.test(wo)
  ok(!anonInOrders, '*** staff orders are NOT anonymized (names visible for calling)')

  const biz = writes.filter((w) => !/\/api\/auth\//.test(w))
  ok(biz.length === 0, 'ZERO business writes: ' + JSON.stringify(biz))
  const real = errs.filter((e) => !/favicon|404|403/i.test(e))
  ok(real.length === 0, 'console errors: ' + real.length +
     (real.length ? ' :: ' + real.slice(0, 2).join(' | ') : ''))
  await ctx.close()
}

// ══════════════════════════════════════════════════════
console.log('\n########## 2. regression: manager ##########')
// ══════════════════════════════════════════════════════
{
  const { page, ctx } = await loginAs('liming')
  const nav = await navText(page)
  console.log('  sidebar:', nav)
  ok(nav.includes('客户名单'), 'manager still sees 客户名单')
  ok(nav.includes('干预策略'), 'manager still sees 干预策略')
  ok(nav.includes('智能助手'), 'manager still sees 智能助手')
  ok(nav.includes('系统管理'), 'manager still sees 系统管理 group')
  ok(nav.includes('客群洞察') && nav.includes('模型效果'), 'manager sees analysis pages')
  ok(nav.includes('数据洞察'), 'manager sees 数据洞察')

  await page.goto(BASE + '/customers', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(6000)
  const t = await page.evaluate(() => document.body.innerText)
  ok(page.url().includes('/customers'), 'manager can open 客户名单')
  ok(/查看全部|全行|客户流失防控/.test(t) || t.length > 300, 'customers page rendered')
  await ctx.close()
}

// ══════════════════════════════════════════════════════
console.log('\n########## 3. regression: viewer ##########')
// ══════════════════════════════════════════════════════
{
  const { page, ctx } = await loginAs('chenjie')
  const nav = await navText(page)
  console.log('  sidebar:', nav)
  ok(nav.includes('客户名单'), 'viewer still sees 客户名单 (insight:view kept)')
  ok(nav.includes('干预策略'), 'viewer still sees 干预策略')
  ok(!nav.includes('智能助手'), 'viewer does not see 智能助手 (no agent:use)')
  ok(!nav.includes('操作审计'), 'viewer does not see 操作审计')

  await page.goto(BASE + '/customers', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(6000)
  const t = await page.evaluate(() => document.body.innerText)
  ok(page.url().includes('/customers'), 'viewer can still open 客户名单')
  ok(/只读分析|仅可查看聚合/.test(t), 'viewer customers page shows its mask notice')
  await ctx.close()
}

const pass = results.filter((r) => r[0]).length
console.log('\n==================================================')
console.log('RESULT: ' + pass + '/' + results.length + ' passed')
console.log('==================================================')
for (const [c, m] of results) if (!c) console.log('  FAILED:', m)
await browser.close()
process.exit(pass === results.length ? 0 : 1)
