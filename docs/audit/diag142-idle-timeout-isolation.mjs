/**
 * 验证**路径 C：无操作超时**的行为。
 *
 * ⚠ 断言已按 v3 实现更新（**不是放宽，是目标变了**）：
 *   v2 的设计是"超时把助手缓存一并删掉"，那时断言"缓存被清除"。
 *   但用户指出那样会导致"重新登录历史就没了" —— 故 v3 改为
 *   **分键隔离 + 登出只清凭据**：
 *       凭据（token / user）→ 必须清（属于当前会话）
 *       对话历史（agent.session.v1.<user>）→ 保留（属于该用户的数据）
 *
 *   故现在该验证的是：
 *     · 令牌被清除（安全要求）
 *     · 跳转到登录页
 *     · **助手历史被保留**（用户诉求）
 *
 * ⚠ 为什么单独写一个脚本：
 *   超时等待是 30 分钟，不适合塞进常规 e2e。这里用 Playwright 的
 *   `page.clock` 把虚拟时间快进 31 分钟，真实触发 setTimeout。
 *
 * ⚠ 关键：clock 必须在**页面加载前** install，否则 AppLayout 的
 *   onMounted 已经用真实时间注册了定时器，快进无效。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, loginAs } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'
const BASE = 'http://localhost:5173'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 950 } })
const page = await ctx.newPage()
await installReadOnlyGuard(page)

const fails = []
const ok = (c, m) => { console.log(`  [${c ? 'OK ' : 'FAIL'}] ${m}`); if (!c) fails.push(m) }

console.log('='.repeat(80))
console.log('准备：liming 登录并在助手页产生对话')

await loginAs(page, 'liming')
await page.goto(`${BASE}/assistant`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2200)

// install 之后新注册的 setTimeout 才受快进控制。
// AppLayout 的 onMounted 在导航时已跑过，故 reload 一次让它在虚拟时钟下挂载。
await page.clock.install()
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2000)

const readKey = () => page.evaluate(() => {
  const key = 'agent.session.v1.liming'
  const raw = localStorage.getItem(key)
  return raw ? JSON.parse(raw) : null
})

let store = await readKey()
if (!store) {
  await page.locator('.composer textarea').fill('现在决策阈值是多少')
  await page.locator('.btn-send').click()
  await page.waitForTimeout(1500)
  for (let i = 0; i < 60; i++) {
    if (await page.locator('.thinking').count() === 0) break
    await page.waitForTimeout(1000)
  }
  await page.waitForTimeout(700)
  store = await readKey()
}
console.log('    专属键 agent.session.v1.liming → turns =', store?.turns?.length)
ok(!!store, '产生了对话缓存（在 liming 的专属键里）')

console.log('\n快进 31 分钟，触发无操作超时')
await page.clock.fastForward('31:00')
await page.waitForTimeout(1500)

const afterIdle = await readKey()
const url = page.url()
const tok = await page.evaluate(() => localStorage.getItem('auth.token.v1'))
console.log('    缓存:', afterIdle === null ? '已清除' : `保留（${afterIdle?.turns?.length} 轮）`)
console.log('    URL:', url)
console.log('    令牌:', tok === null ? '已清除' : '(仍在)')

ok(url.includes('/login'), '⚠ 被重定向到登录页（会话已失效）')
ok(tok === null, '⚠ 身份凭据被清除（安全要求）')
ok(afterIdle !== null, '⚠⚠ 助手历史**被保留**（用户诉求：重新登录还在）')

console.log('\n重新登录 → 历史应恢复')
await loginAs(page, 'liming')
await page.goto(`${BASE}/assistant`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2600)
const t = await page.locator('.turn').count()
console.log('    重新登录后轮数:', t)
ok(t > 0, '⚠ 超时后重新登录，自己的历史仍在')

console.log('\n' + '='.repeat(80))
console.log('结论：' + (fails.length === 0
  ? 'PASS —— 超时清凭据但保留历史，重新登录可恢复'
  : `FAIL —— ${fails.length} 项未通过`))
fails.forEach(f => console.log('   × ' + f))
await ctx.close()
await browser.close()

