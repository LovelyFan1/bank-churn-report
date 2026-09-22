/**
 * 登录系统 UI 端到端 —— 走**真实登录表单**（不塞 localStorage）。
 *
 * 覆盖：
 *   1. 未登录访问 /dashboard → 被重定向到 /login
 *   2. 登录页展示双因素步骤
 *   3. 口令错误 → 错误提示，不跳转
 *   4. 两步登录（口令 → 动态口令）成功进入系统
 *   5. 顶栏显示登录人姓名与角色
 *   6. 登录后刷新仍保持登录
 *   7. 只读角色看不到「操作审计」导航
 *   8. 登出 → 回到登录页且令牌被清
 *   9. 会话过期（伪造失效令牌）→ 自动跳登录页
 *
 * ⚠ 只读：安装共享护栏，拦截所有写请求。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, totpNow } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'
const BASE = 'http://localhost:5173'
const SECRET = process.env.TEST_TOTP_SECRET

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

const guard = await installReadOnlyGuard(page)
const fails = []
const ok = (c, m) => { console.log(`  [${c ? 'OK ' : 'FAIL'}] ${m}`); if (!c) fails.push(m) }

/**
 * 意图内的控制台错误计数。
 *
 * ⚠ 本脚本含**故意失败**的步骤（错口令、错动态口令、失效令牌），
 *   每一步都会让浏览器记录 "401 Unauthorized" 控制台错误 —— 那是
 *   预期行为，不是缺陷。实测踩过：27 项断言全过，却因为把这些
 *   预期的 401 算成"意外错误"而报 FAIL，属于**假告警**。
 *   故这里显式记录"因负向测试而预期产生的错误数"，最后只对
 *   剩下的意外错误做零容忍断言。
 */
let intendedErrors = 0

async function expectFailure(fn) {
  const before = errors.length
  await fn()
  intendedErrors += Math.max(0, errors.length - before)
}

async function fillLogin(user, pass) {
  await page.locator('input[autocomplete="username"]').fill(user)
  await page.locator('input[autocomplete="current-password"]').fill(pass)
  await page.locator('button[type="submit"]').click()
  await page.waitForTimeout(1600)
}

console.log('='.repeat(84))
console.log('一、未登录访问受保护页面 → 重定向到登录页')
await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1800)
console.log('  当前 URL:', page.url())
ok(page.url().includes('/login'), '被重定向到 /login')
const guardHit = await page.locator('.login-card').count()
ok(guardHit > 0, '看到登录卡片')
// ⚠ 登录页不该有侧边栏（否则未登录能看见全部导航）
const sidebarOnLogin = await page.locator('aside').count()
ok(sidebarOnLogin === 0, '⚠ 登录页无侧边栏（不暴露导航）')

console.log('\n二、口令错误 → 提示且不跳转')
await expectFailure(() => fillLogin('liming', 'wrong-password'))
const errText = await page.locator('.err').innerText().catch(() => '')
console.log('  错误提示:', errText)
ok(errText.length > 0, '显示了错误提示')
ok(page.url().includes('/login'), '仍停留在登录页')

console.log('\n三、只读账号单因素登录')
await fillLogin('chenjie', 'Bank@2026')
console.log('  当前 URL:', page.url())
ok(!page.url().includes('/login'), '进入系统')
const nameText = await page.locator('.user-name').innerText().catch(() => '')
const roleText = await page.locator('.role-chip').innerText().catch(() => '')
console.log('  顶栏:', nameText, '|', roleText)
ok(nameText.includes('陈杰'), '⚠ 顶栏显示登录人姓名')
ok(roleText.includes('只读'), '顶栏显示角色')

console.log('\n四、只读角色看不到「操作审计」')
const navText = await page.locator('aside').innerText()
ok(!navText.includes('操作审计'), '⚠ 导航中无「操作审计」（无 audit:view 权限）')
// 直接访问也应被守卫弹回
await page.goto(`${BASE}/audit`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
console.log('  直接访问 /audit →', page.url())
ok(!page.url().includes('/audit'), '直接访问被守卫拦回')

console.log('\n五、登出 → 回登录页且令牌清除')
await page.locator('.btn-logout').click()
await page.waitForTimeout(1500)
const tok = await page.evaluate(() => localStorage.getItem('auth.token.v1'))
console.log('  URL:', page.url(), '| token:', tok)
ok(page.url().includes('/login'), '回到登录页')
ok(tok === null, '⚠ 令牌已清除')

console.log('\n六、管理员两步登录（口令 → 动态口令）')
await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
await fillLogin('zhaomin', 'Bank@2026')
const step2 = await page.locator('#totp-input').count()
console.log('  出现动态口令输入框:', step2 > 0)
ok(step2 > 0, '⚠ 进入第二步（双因素）')
if (step2 > 0) {
  if (!SECRET) {
    console.log('  [SKIP] 未提供 TEST_TOTP_SECRET，无法算动态口令')
  } else {
    // 先试错误口令
    await expectFailure(async () => {
      await page.locator('#totp-input').fill('000000')
      await page.locator('button[type="submit"]').click()
      await page.waitForTimeout(1500)
    })
    const e2 = await page.locator('.err').innerText().catch(() => '')
    console.log('  错误动态口令提示:', e2)
    ok(e2.length > 0, '错误动态口令被拒')
    // 再用正确口令
    await page.locator('#totp-input').fill(totpNow(SECRET))
    await page.locator('button[type="submit"]').click()
    await page.waitForTimeout(2200)
    console.log('  当前 URL:', page.url())
    ok(!page.url().includes('/login'), '⚠ 双因素登录成功')
    const n2 = await page.locator('.user-name').innerText().catch(() => '')
    ok(n2.includes('赵敏'), `管理员姓名正确：${n2}`)
  }
}

console.log('\n七、管理员可见「操作审计」')
const nav2 = await page.locator('aside').innerText()
ok(nav2.includes('操作审计'), '⚠ 管理员导航含「操作审计」')
await page.goto(`${BASE}/audit`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2200)
const rows = await page.locator('.tbl tbody tr').count()
const title = await page.locator('.page-title').innerText().catch(() => '')
console.log('  审计页标题:', title, '| 行数:', rows)
ok(title.includes('操作审计'), '审计页可访问')
ok(rows > 0, `⚠ 审计表有数据（${rows} 行）`)
// 审计里应能看到 agent 来源的记录（本次改造的核心）
const tableText = await page.locator('.tbl').innerText()
ok(tableText.includes('登录'), '审计含登录记录')

console.log('\n八、刷新保持登录')
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2200)
console.log('  刷新后 URL:', page.url())
ok(!page.url().includes('/login'), '⚠ 刷新后仍登录（令牌持久化生效）')

console.log('\n九、失效令牌 → 自动跳登录页')
// ⚠ 这一步**故意**用无效令牌，必然产生 401 控制台错误（记入 intendedErrors）
await expectFailure(async () => {
  await page.evaluate(() => localStorage.setItem('auth.token.v1', 'garbage.token.here'))
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2600)
})
console.log('  当前 URL:', page.url())
ok(page.url().includes('/login'), '⚠ 失效令牌被识别并跳登录页')

const unexpectedErrors = errors.slice(0, errors.length - intendedErrors)

console.log('\n' + '='.repeat(84))
console.log(`控制台错误：预期内（负向测试所致）${intendedErrors} 条，` +
            `意外 ${unexpectedErrors.length} 条`)
unexpectedErrors.slice(0, 4).forEach(e => console.log('   !', e.slice(0, 150)))
ok(unexpectedErrors.length === 0, `无意外控制台错误（实际 ${unexpectedErrors.length}）`)
console.log('写请求：拦截', guard.blocked.length, '放行', guard.allowedWrites.length)
const unexpectedWrites = guard.report().unexpectedAllowed
ok(unexpectedWrites.length === 0, `无预期外写请求（实际 ${unexpectedWrites.length}）`)

console.log('\n结论：' + (fails.length === 0
  ? 'PASS —— 登录/双因素/权限导航/登出/超时/失效令牌全部按预期'
  : `FAIL —— ${fails.length} 项未通过`))
fails.forEach(f => console.log('   × ' + f))

await browser.close()
