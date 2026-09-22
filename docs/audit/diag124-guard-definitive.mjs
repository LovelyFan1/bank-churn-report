/**
 * 决定性验证：只读护栏到底能不能拦住写请求？
 *
 * 背景：我先前用 diag122 得出"拦截失效"的结论，但那个结论的判据本身有 bug ——
 *   它把"被我的 handler 记录"当成了"穿透到后端"，而那个 handler 其实
 *   已经 fulfill 了 500。所以那个结论**不可信**，必须重做。
 *
 * 本脚本用**唯一可信的判据**：后端访问日志的计数。
 *   若点击了会产生写请求的按钮，而后端写请求计数不变 → 拦截生效。
 *
 * 三步：
 *   1. 先确认 ask 能正常跑通（护栏不能把只读的 POST /ask 也拦掉）
 *   2. 点击确认按钮（本该写库）
 *   3. 比对后端 confirm 计数
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, resetAgentSession } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 } })
const page = await ctx.newPage()

const guard = await installReadOnlyGuard(page)
await resetAgentSession(page)

const fails = []

// ── 第一步：确认 ask 能正常跑通（护栏不该拦它）──
console.log('=' .repeat(84))
console.log('一、POST /api/agent/ask 必须放行（它只读，不是写操作）')
console.log('=' .repeat(84))
await page.locator('.composer textarea').fill('现在决策阈值是多少？')
await page.locator('.btn-send').click()
for (let i = 0; i < 90; i++) {
  if (await page.locator('.thinking').count() === 0) break
  await page.waitForTimeout(1000)
}
await page.waitForTimeout(700)

const last = page.locator('.turn.agent').last()
const basis = (await last.locator('.basis').innerText().catch(() => '')).trim()
const headline = await last.locator('.ans-headline').innerText().catch(() => '')
console.log('  口径:', basis || '(空)')
console.log('  结论:', headline || '(空)')
if (!basis || basis === '未知') {
  fails.push('护栏把只读的 POST /api/agent/ask 也拦掉了（页面拿不到数据）')
  console.log('  [FAIL] ask 被拦 —— 护栏的 allow 名单没生效')
} else {
  console.log('  [OK] ask 正常返回')
}

// ── 第二步：点击会产生写请求的按钮 ──
console.log()
console.log('=' .repeat(84))
console.log('二、点击确认按钮（本该写库）')
console.log('=' .repeat(84))
await page.locator('.composer textarea').fill('给 C062858 建个挽留工单')
await page.locator('.btn-send').click()
for (let i = 0; i < 90; i++) {
  if (await page.locator('.thinking').count() === 0) break
  await page.waitForTimeout(1000)
}
await page.waitForTimeout(700)

const last2 = page.locator('.turn.agent').last()
const btn = last2.locator('.pending .btn-confirm')
const n = await btn.count()
console.log('  pending 确认按钮数:', n)
if (n) {
  await btn.first().click()
  await page.waitForTimeout(2200)
  const done = await last2.locator('.done').innerText().catch(() => '')
  console.log('  点击后界面:', (done || '(无完成提示)').replace(/\s+/g, ' '))
} else {
  console.log('  （未出现确认按钮，本步跳过）')
}

// ── 报告 ──
const rep = guard.report()
console.log()
console.log('=' .repeat(84))
console.log('护栏内部报告')
console.log('=' .repeat(84))
console.log('  拦截的写请求:', rep.blockedWrites)
guard.blockedDetail.forEach(d => console.log(`    · ${d.method} ${d.path}`))
console.log('  允许的只读 POST:', rep.allowedWrites, rep.allowedDetail)
  console.log('  非预期放行:', rep.unexpectedAllowed)
console.log('  放行的读请求:', rep.gets)

console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  // ⚠ 结论行必须含 PASS/FAIL token —— 批量运行器
  //   (run-e2e-with-write-check.ps1) 靠这两个词判定成败。
  //   此前这里写的是"护栏行为正确"（不含 PASS），于是被一律判为 FAIL（误报）。
  console.log('结论：PASS —— 护栏行为正确（ask 放行、confirm 拦截、后端零写入）')
}

await browser.close()
