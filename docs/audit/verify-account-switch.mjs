/**
 * 验证：切换账号后，行员名单与权限不残留。
 * 只读（不建单、不改单、不删单），断言业务写请求为 0。
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'https://blessed-ghz-fast-francis.trycloudflare.com'
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1500, height: 950 } })
const page = await ctx.newPage()
const writes = []
page.on('request', (r) => {
  const m = r.method().toUpperCase()
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m)) writes.push(m + ' ' + new URL(r.url()).pathname)
})

async function doLogin(u) {
  await page.goto(BASE + '/login', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(1200)
  await page.locator('input[autocomplete="username"]').fill(u)
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
  await page.waitForTimeout(2500)
}

// 1) 管理员登录（can_assign = true 的名单）
console.log('=== login as liming (manager) ===')
await doLogin('liming')
await page.goto(BASE + '/customers', { waitUntil: 'networkidle', timeout: 60000 })
await page.waitForTimeout(5000)
const mgrNav = await page.evaluate(() => document.querySelector('aside')?.innerText.replace(/\s+/g, ' ') || '')
ok(mgrNav.includes('智能助手'), 'manager sees 智能助手')

// 2) 登出 → 用 staff 登录
console.log('\n=== logout, then login as wangxiaoyun (staff) ===')
await page.locator('button:has-text("退出")').first().click().catch(() => {})
await page.waitForTimeout(2500)
if (page.url().includes('/login')) {
  ok(true, 'logout redirected to /login')
} else {
  await page.evaluate(() => {
    localStorage.removeItem('auth.token.v1')
    localStorage.removeItem('auth.user.v1')
  })
  ok(false, 'logout did not redirect (fell back to manual clear)')
}
await doLogin('wangxiaoyun')

// 3) staff 的导航**不应**出现智能助手 —— 若名单/权限残留会错误出现
const staffNav = await page.evaluate(() => document.querySelector('aside')?.innerText.replace(/\s+/g, ' ') || '')
console.log('   staff nav:', staffNav.slice(0, 90))
ok(!staffNav.includes('智能助手'), 'staff does NOT see 智能助手 after account switch (no residue)')

// 4) staff 进工单页：负责人应为只读，且显示的是"自己"
await page.goto(BASE + '/work-orders', { waitUntil: 'networkidle', timeout: 60000 })
await page.waitForTimeout(6000)
const wo = await page.evaluate(() => document.body.innerText)
ok(/我的工单/.test(wo), 'staff sees 「我的工单」')
ok(/王晓芸/.test(wo), 'staff own name rendered')
ok(!/wangxiaoyun/.test(wo), 'no raw username leaked')

// 5) staff 打开编辑弹窗：负责人应是**只读 input**（can_assign=false）
const editBtn = page.locator('button:has-text("✎")').first()
if (await editBtn.count()) {
  await editBtn.click()
  await page.waitForTimeout(2000)
  const info = await page.evaluate(() => {
    const modal = document.querySelector('.modal')
    if (!modal) return null
    const groups = Array.from(modal.querySelectorAll('.form-group'))
    const g = groups.find((x) => /负责人/.test(x.innerText))
    if (!g) return { found: false }
    return {
      found: true,
      isSelect: !!g.querySelector('select'),
      isReadonly: !!g.querySelector('input[readonly]'),
      text: g.innerText.replace(/\s+/g, ' ').trim(),
    }
  })
  console.log('   assignee field for staff:', JSON.stringify(info))
  ok(info && info.found && !info.isSelect && info.isReadonly,
     'staff sees 负责人 as READ-ONLY (cannot reassign)')
  await page.locator('.modal .modal-close').click()
  await page.waitForTimeout(500)
}

const bizWrites = writes.filter((w) => !/\/api\/auth\//.test(w))
console.log('\n   business writes:', JSON.stringify(bizWrites))
ok(bizWrites.length === 0, 'ZERO business writes')

const pass = results.filter((r) => r[0]).length
console.log('\nRESULT: ' + pass + '/' + results.length + ' passed')
for (const [c, m] of results) if (!c) console.log('  FAILED:', m)
await browser.close()
process.exit(pass === results.length ? 0 : 1)
