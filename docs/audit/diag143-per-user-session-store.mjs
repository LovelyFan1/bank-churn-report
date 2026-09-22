/**
 * 验证：**按用户分键隔离** —— 既不让别人看到，也不丢自己的。
 *
 * 这个脚本覆盖用户提出的两轮反馈：
 *   第一轮：「切换账户了上个账户的助手对话历史还在」（隐私缺陷）
 *   第二轮：「一个账户聊了的记录再登录的时候就会清空呀」（我修过头了）
 *
 * 故核心断言是**三条同时成立**：
 *   A. A 登录 → 有历史
 *   B. 换 B 登录 → B 看不到 A 的内容（隔离）
 *   C. 换回 A 登录 → **A 的历史还在**（不丢）
 *
 * ⚠ 只读：只发 /agent/ask（架构保证该接口不写库）。
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

/** 登录并在助手页问一句，返回该页看到的轮数 */
async function askAndCount(user, question) {
  await loginAs(page, user)
  await page.goto(`${BASE}/assistant`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2200)
  if (question) {
    await page.locator('.composer textarea').fill(question)
    await page.locator('.btn-send').click()
    await page.waitForTimeout(1400)
    for (let i = 0; i < 60; i++) {
      if (await page.locator('.thinking').count() === 0) break
      await page.waitForTimeout(1000)
    }
    await page.waitForTimeout(700)
  }
  return await page.locator('.turn').count()
}

/** 列出 localStorage 里所有 agent.session 相关的键 */
const listKeys = () => page.evaluate(() => {
  const out = []
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i)
    if (k && k.startsWith('agent.session')) out.push(k)
  }
  return out.sort()
})

async function logout() {
  await page.locator('.btn-logout').click()
  await page.waitForTimeout(1600)
}

console.log('='.repeat(84))
console.log('一、A（liming）登录并产生历史')
const t1 = await askAndCount('liming', '现在决策阈值是多少')
console.log('    liming 看到轮数:', t1)
ok(t1 > 0, 'A 产生了对话历史')
let keys = await listKeys()
console.log('    localStorage 键:', JSON.stringify(keys))
ok(keys.some(k => k.endsWith('.liming')), '⚠ 缓存在**带 liming 后缀**的专属键里')
ok(!keys.includes('agent.session.v1'), '不再使用无后缀的共用键')

console.log('\n二、登出 → A 的历史必须**保留**（这是上一版修错的地方）')
await logout()
keys = await listKeys()
console.log('    登出后键:', JSON.stringify(keys))
ok(keys.some(k => k.endsWith('.liming')), '⚠ 登出后 A 的历史**仍在**（未被删除）')

console.log('\n三、换 B（wangfang）登录 → 看不到 A 的内容（隔离）')
await loginAs(page, 'wangfang')
await page.goto(`${BASE}/assistant`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
const t2 = await page.locator('.turn').count()
const body2 = await page.locator('body').innerText()
console.log('    wangfang 看到轮数:', t2)
ok(t2 === 0, '⚠ B 看到 0 轮（看不到 A 的历史）')
ok(!/决策阈值 0\.6/.test(body2), '看不到 A 的回答内容')
keys = await listKeys()
console.log('    此时的键:', JSON.stringify(keys))
ok(keys.some(k => k.endsWith('.liming')), 'A 的键仍在（未被 B 覆盖或删除）')

console.log('\n四、给 B 也造一段历史 → 两人各自的键并存')
const t3 = await askAndCount('wangfang', '工单处理得怎么样')
console.log('    wangfang 现在看到轮数:', t3)
ok(t3 > 0, 'B 产生了自己的历史')
keys = await listKeys()
console.log('    键:', JSON.stringify(keys))
ok(keys.some(k => k.endsWith('.liming')), 'A 的键仍在')
ok(keys.some(k => k.endsWith('.wangfang')), '⚠ B 的键独立存在（两人并存）')

console.log('\n五、登出 B、换回 A → A 的历史必须**还在**（用户的核心诉求）')
await logout()
const t4 = await askAndCount('liming', null)   // 只登录进助手页，不提问
console.log('    liming 重新登录后看到轮数:', t4)
ok(t4 > 0, '⚠⚠ A 重新登录后**自己的历史仍在**（未被清空）')
const body4 = await page.locator('body').innerText()
ok(/决策阈值 0\.6/.test(body4), '⚠ 能看到自己上次的回答内容')
ok(!/工单处理得怎么样/.test(body4), '看不到 B 的对话内容')

console.log('\n六、清空会话只影响当前用户')
await page.locator('button:has-text("清空")').first().click()
await page.waitForTimeout(1400)
const t5 = await page.locator('.turn').count()
keys = await listKeys()
console.log('    清空后轮数:', t5, '| 键:', JSON.stringify(keys))
ok(t5 === 0, '当前用户会话已清空')
ok(keys.some(k => k.endsWith('.wangfang')), '⚠ 只清了自己的，B 的历史不受影响')

console.log('\n' + '='.repeat(84))
console.log('结论：' + (fails.length === 0
  ? 'PASS —— 分键隔离：互相看不到，各自历史都保留，清空只影响自己'
  : `FAIL —— ${fails.length} 项未通过`))
fails.forEach(f => console.log('   × ' + f))
await ctx.close()
await browser.close()
