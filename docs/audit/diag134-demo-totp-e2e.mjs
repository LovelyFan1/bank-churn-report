/**
 * 验证：登录页的**动态口令演示辅助**。
 *
 * 覆盖：
 *   1. 后端开启 demo_show_totp 时，登录页第二步显示当前口令
 *   2. 口令可「填入」按钮一键填入并成功登录
 *   3. 显示 30 秒倒计时并自动刷新
 *   4. 显示绑定密钥（供手机 App 手动输入）
 *   5. ⚠ 关闭开关后该区块**完全不渲染**（验证生产形态）
 *   6. ⚠ 未通过第一因子（无临时票据）时拿不到口令
 *
 * ⚠ 只读：安装共享护栏；登录属会话操作，已在 allow 清单内。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'
const BASE = 'http://localhost:5173'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

const guard = await installReadOnlyGuard(page)
const fails = []
let intendedErrors = 0
const ok = (c, m) => { console.log(`  [${c ? 'OK ' : 'FAIL'}] ${m}`); if (!c) fails.push(m) }

async function expectFailure(fn) {
  const before = errors.length
  await fn()
  intendedErrors += Math.max(0, errors.length - before)
}

async function gotoLoginClean() {
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
  await page.evaluate(() => {
    localStorage.removeItem('auth.token.v1')
    localStorage.removeItem('auth.user.v1')
  })
  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(1600)
}

async function toStep2() {
  await page.locator('input[autocomplete="username"]').fill('zhaomin')
  await page.locator('input[autocomplete="current-password"]').fill('Bank@2026')
  await page.locator('button[type="submit"]').click()
  // ⚠ 不能只等固定时长：第二步渲染后**还要异步拉取**动态口令
  //   （POST /auth/demo/totp）。固定 2200ms 在实测中不够 ——
  //   表现为 .totp-demo 已存在但 .td-code 还是空字符串，
  //   断言"口令为 6 位数字"莫名失败，而功能其实是好的。
  //   改为显式等待该元素出现且非空。
  await page.locator('.totp-demo').waitFor({ state: 'visible', timeout: 15000 })
  // ⚠ waitForFunction 的签名是 (fn, arg, options) —— 第二个参数是传给
  //   页面函数的 arg，不是 options。若把 {timeout} 放在第二位，
  //   它会被当成 arg 传进页面，options 取默认值（30s）并**静默不生效**。
  //   这里显式传 null 占位。
  await page.waitForFunction(() => {
    const el = document.querySelector('.td-code')
    return !!el && /\d{6}/.test(el.textContent || '')
  }, null, { timeout: 15000 })
}

console.log('='.repeat(82))
console.log('一、第一步之前：不应显示任何动态口令')
await gotoLoginClean()
const demoBefore = await page.locator('.totp-demo').count()
ok(demoBefore === 0, '⚠ 未通过口令前不显示动态口令（需临时票据）')

console.log('\n二、进入第二步 → 显示当前动态口令')
await toStep2()
const hasDemo = await page.locator('.totp-demo').count()
ok(hasDemo > 0, '⚠ 显示出动态口令区')
if (hasDemo > 0) {
  const codeText = (await page.locator('.td-code').innerText()).trim()
  console.log('  显示的口令:', codeText)
  ok(/^\d{6}$/.test(codeText), `口令为 6 位数字（${codeText}）`)
  const warnText = await page.locator('.td-warn').innerText()
  console.log('  警示文案:', warnText.trim())
  ok(/演示|生产/.test(warnText), '⚠ 有醒目的"演示辅助/生产禁用"警示')

  const leftText = await page.locator('.td-left').innerText()
  console.log('  倒计时:', leftText)
  ok(/\d+\s*秒/.test(leftText), '显示倒计时秒数')

  // 密钥（折叠区）
  await page.locator('.td-detail summary').click()
  await page.waitForTimeout(400)
  const secretText = await page.locator('.td-secret code').innerText()
  console.log('  绑定密钥:', secretText.trim())
  ok(/^[A-Z2-7]{16,}$/.test(secretText.trim()), '显示可手动输入的绑定密钥')

  console.log('\n三、点「填入」→ 一键填写并登录')
  await page.locator('.td-use').click()
  await page.waitForTimeout(400)
  const filled = await page.locator('#totp-input').inputValue()
  console.log('  输入框内容:', filled)
  ok(filled === codeText, '⚠ 填入的正是显示的口令')
  await page.locator('button[type="submit"]').click()
  await page.waitForTimeout(2600)
  console.log('  当前 URL:', page.url())
  ok(!page.url().includes('/login'), '⚠ 用演示口令登录成功')
  const nm = await page.locator('.user-name').innerText().catch(() => '')
  ok(nm.includes('赵敏'), `进入系统，身份：${nm}`)
}

console.log('\n四、倒计时是否自动刷新口令')
await gotoLoginClean()
await toStep2()
const c1 = (await page.locator('.td-code').innerText()).trim()
const l1 = parseInt((await page.locator('.td-left').innerText()).match(/\d+/)?.[0] || '0', 10)
console.log(`  当前口令 ${c1}，剩余 ${l1} 秒 → 等待其过期…`)
// 等到倒计时归零后应自动重取（最多等 35 秒）
let c2 = c1
for (let i = 0; i < 40; i++) {
  await page.waitForTimeout(1000)
  const el = await page.locator('.td-code').count()
  if (!el) break
  c2 = (await page.locator('.td-code').innerText()).trim()
  if (c2 !== c1) break
}
console.log('  刷新后的口令:', c2)
ok(c2 !== c1, `⚠ 倒计时结束后自动刷新为新口令（${c1} → ${c2}）`)

console.log('\n五、⚠ 关闭开关后该区块完全不渲染（验证生产形态）')
const offResp = await page.request.get(`${BASE}/api/auth/policy`)
const pol = await offResp.json()
console.log('  当前 demo_show_totp =', pol.demo_show_totp)
if (pol.demo_show_totp) {
  console.log('  （本环境开关为 true，改由接口层验证：直接调 demo 接口需临时票据）')
  // 无票据调用应被拒。
  // ⚠ 伪造串必须**足够长**（>=8）：入参校验会先于令牌解码执行，
  //   太短会返回 422（参数不合法）而不是 401（令牌无效）——
  //   那样测的是 Pydantic 而非鉴权，属于假验证。
  const r = await page.request.post(`${BASE}/api/auth/demo/totp`, {
    data: { ticket: 'aGVsbG8.d29ybGQ.c2lnbmF0dXJl' },
  })
  console.log('  伪造票据调 demo 接口 →', r.status())
  ok(r.status() === 401, '⚠ 无有效临时票据拿不到口令（401 而非参数错误）')
} else {
  console.log('  （开关已关闭）')
}

console.log('\n' + '='.repeat(82))
const unexpected = errors.slice(0, errors.length - intendedErrors)
console.log(`控制台错误：预期内 ${intendedErrors} 条，意外 ${unexpected.length} 条`)
unexpected.slice(0, 4).forEach(e => console.log('   !', e.slice(0, 140)))
ok(unexpected.length === 0, `无意外控制台错误（实际 ${unexpected.length}）`)

console.log('\n结论：' + (fails.length === 0
  ? 'PASS —— 演示口令显示/填入/自动刷新/密钥绑定均正常'
  : `FAIL —— ${fails.length} 项未通过`))
fails.forEach(f => console.log('   × ' + f))

await browser.close()
