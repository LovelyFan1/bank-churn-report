/**
 * 验证：关闭 AUTH_DEMO_SHOW_TOTP 后，登录页**完全不渲染**演示区。
 * 这是"生产形态"的回归断言 —— 开关默认必须是关的。
 */
import { chromium } from 'playwright-core'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'
const BASE = 'http://localhost:5173'
const browser = await chromium.launch({ executablePath: EXE, headless: true })
const page = await (await browser.newContext()).newPage()

const fails = []
const ok = (c, m) => { console.log(`  [${c ? 'OK ' : 'FAIL'}] ${m}`); if (!c) fails.push(m) }

await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
await page.evaluate(() => {
  localStorage.removeItem('auth.token.v1')
  localStorage.removeItem('auth.user.v1')
})
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(1500)

const pol = await (await page.request.get(`${BASE}/api/auth/policy`)).json()
console.log('  policy.demo_show_totp =', pol.demo_show_totp)
ok(pol.demo_show_totp === false, '开关处于关闭状态（默认值）')

await page.locator('input[autocomplete="username"]').fill('zhaomin')
await page.locator('input[autocomplete="current-password"]').fill('Bank@2026')
await page.locator('button[type="submit"]').click()
await page.waitForTimeout(3000)

const step2 = await page.locator('#totp-input').count()
console.log('  仍在第二步（动态口令输入框）:', step2 > 0)
ok(step2 > 0, '登录流程仍要求动态口令')

const demoCount = await page.locator('.totp-demo').count()
const codeCount = await page.locator('.td-code').count()
const secretCount = await page.locator('.td-secret').count()
console.log(`  演示区元素: .totp-demo=${demoCount} .td-code=${codeCount} .td-secret=${secretCount}`)
ok(demoCount === 0, '⚠ 演示区完全不渲染')
ok(codeCount === 0, '⚠ 不显示任何动态口令')
ok(secretCount === 0, '⚠ 不显示绑定密钥')

// 接口层也应拒绝
const r = await page.request.post(`${BASE}/api/auth/demo/totp`, {
  data: { ticket: 'aGVsbG8.d29ybGQ.c2ln' },
})
console.log('  关闭后调 demo 接口 →', r.status())
ok(r.status() === 404, '⚠ 接口返回 404（不暴露该功能存在）')

console.log('\n结论：' + (fails.length === 0
  ? 'PASS —— 开关关闭时演示辅助完全不可见（生产形态正确）'
  : `FAIL —— ${fails.length} 项`))
fails.forEach(f => console.log('   × ' + f))
await browser.close()
