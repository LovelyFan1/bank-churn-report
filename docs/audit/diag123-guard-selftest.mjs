/**
 * 验证共享只读护栏真的能拦住写请求。
 *
 * ⚠ 这是被真实事故逼出来的测试：diag118 的拦截失效，导致用户手工建的
 *   工单 #63 被真实删除。本脚本用**后端日志计数**作为独立判据 ——
 *   不能只信"我以为拦住了"，必须证明后端没收到请求。
 *
 * 方法：点击一个会产生写请求的按钮，然后比对后端 confirm 计数。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 } })
const page = await ctx.newPage()

const guard = await installReadOnlyGuard(page)

await page.goto('http://localhost:5173/assistant', { waitUntil: 'networkidle' })
await page.waitForTimeout(2000)
await page.evaluate(() => localStorage.removeItem('agent.session.v1'))
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2200)

// 走批量建单路径：它最容易触发写请求，也是之前漏拦的那种
await page.locator('.composer textarea').fill('挽回价值最高的3个客户')
await page.locator('.btn-send').click()
for (let i = 0; i < 90; i++) {
  if (await page.locator('.thinking').count() === 0) break
  await page.waitForTimeout(1000)
}
await page.waitForTimeout(800)

const last = page.locator('.turn.agent').last()
const act = last.locator('.btn-action')
console.log('动作按钮数:', await act.count())

if (await act.count()) {
  await act.first().click()
  await page.waitForTimeout(900)
  const panel = last.locator('.batch-panel')
  if (await panel.count()) {
    console.log('批量面板已打开')
    await panel.locator('.btn-confirm').first().click()
    await page.waitForTimeout(2000)
    const res = (await last.locator('.batch-result').innerText().catch(() => ''))
      .replace(/\s+/g, ' ')
    console.log('结果区:', res.slice(0, 110))
  }
}

// 再走一次单条确认（取消工单路径）
await page.locator('.composer textarea').fill('帮我给 C071081 建个挽留工单')
await page.locator('.btn-send').click()
for (let i = 0; i < 90; i++) {
  if (await page.locator('.thinking').count() === 0) break
  await page.waitForTimeout(1000)
}
await page.waitForTimeout(700)
const last2 = page.locator('.turn.agent').last()
const cb = last2.locator('.pending .btn-confirm')
if (await cb.count()) {
  await cb.first().click()
  await page.waitForTimeout(1800)
  console.log('单条确认已点击')
}

const rep = guard.report()
console.log()
console.log('=' .repeat(84))
console.log('护栏报告（本脚本内部）:')
console.log('  拦截的写请求:', rep.blockedWrites)
console.log('  允许的只读 POST:', rep.allowedWrites, rep.allowedDetail)
  console.log('  非预期放行:', rep.unexpectedAllowed)
console.log('  放行的读请求:', rep.gets)
console.log('  安全:', rep.safe)
console.log('=' .repeat(84))
console.log()
console.log('⚠ 还需在后端日志侧独立核对计数（见调用方脚本）')

if (!rep.safe) {
  console.log('结论：FAIL —— 有写请求穿透护栏')
  process.exitCode = 2
} else {
  console.log('结论：护栏内部判定安全（拦截 ' + rep.blockedWrites + ' 条写请求）')
}

await browser.close()
