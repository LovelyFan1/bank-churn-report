/**
 * 自检：diag118 的 route 拦截是否真的生效？
 *
 * 背景：数据库里 #63 被删了，日志显示来源是 172.18.0.4（浏览器经 vite 代理），
 * 时间 10:45:38，而我的脚本都跑在容器内（127.0.0.1）。
 * 但**不能假设**，必须实测：如果我的 route pattern 没匹配上，
 * 那我的测试就在真实删库 —— 这是最严重的问题。
 *
 * 本脚本做一件很直接的事：
 *   1. 用与 diag118 完全相同的 route pattern 拦截 confirm
 *   2. 记录拦截是否被调用
 *   3. 同时在后端日志里查是否出现真实的 confirm 请求
 *
 * 判据：若拦截被调用且日志无新增 confirm → 拦截有效（#63 不是我的测试删的）
 *       若日志出现 confirm → 我的测试在删库，必须立刻修
 */
import { chromium } from 'playwright-core'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } })
const page = await ctx.newPage()

const intercepted = []
const passedThrough = []

// 与 diag118 完全相同的 pattern
await page.route('**/api/agent/confirm', route => {
  intercepted.push(route.request().url())
  return route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ created: false, deleted: true, order_id: 999999 }),
  })
})
// 记录所有其它写请求，确认没有漏网的
await page.route('**/api/**', route => {
  const m = route.request().method()
  if (m !== 'GET') {
    passedThrough.push(`${m} ${route.request().url()}`)
    if (route.request().url().includes('/confirm')) {
      // 不该走到这里
      return route.fulfill({ status: 500, body: '{"detail":"LEAKED"}' })
    }
  }
  return route.continue()
})

await page.goto('http://localhost:5173/assistant', { waitUntil: 'networkidle' })
await page.waitForTimeout(2000)
await page.evaluate(() => localStorage.removeItem('agent.session.v1'))
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2200)

// 直接问一个会产出 pending 的问题（建单），然后点确认 —— 看是否被拦截
await page.locator('.composer textarea').fill('给 C062858 建个挽留工单')
await page.locator('.btn-send').click()
for (let i = 0; i < 90; i++) {
  if (await page.locator('.thinking').count() === 0) break
  await page.waitForTimeout(1000)
}
await page.waitForTimeout(800)

const last = page.locator('.turn.agent').last()
const btn = last.locator('.pending .btn-confirm')
const n = await btn.count()
console.log('pending 确认按钮数:', n)

if (n) {
  await btn.first().click()
  await page.waitForTimeout(2000)
  const done = await last.locator('.done').innerText().catch(() => '')
  console.log('点击后界面显示:', done.replace(/\s+/g, ' '))
}

console.log()
console.log('=' .repeat(80))
console.log('拦截到的 /api/agent/confirm 请求数:', intercepted.length)
intercepted.forEach(u => console.log('  ·', u.replace(/^https?:\/\/[^/]+/, '')))
console.log('透传的非 GET 请求:', passedThrough.length)
passedThrough.forEach(u => console.log('  ·', u.replace(/^https?:\/\/[^/]+/, '')))
console.log('=' .repeat(80))

if (passedThrough.some(u => u.includes('/confirm'))) {
  console.log('结论：**拦截失效** —— 有 confirm 请求穿透到了后端！')
  process.exitCode = 2
} else if (intercepted.length) {
  console.log('结论：拦截有效 —— confirm 请求被 route 截住，未到后端')
} else {
  console.log('结论：本次未产生 confirm 请求（无法判定，需人工核对日志）')
}

await browser.close()
