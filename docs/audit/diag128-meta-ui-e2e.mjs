/**
 * 前端核验：元问题在界面上的呈现。
 *
 * 关注三点：
 *   1. 口径徽章显示为「系统信息 · 确定性回答」（不再是"数字已校验"）
 *   2. 能力清单以 facts 列表渲染（9 个工具 + 3 类答不了）
 *   3. 界面无 Markdown 星号裸露
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, resetAgentSession } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1200 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

const guard = await installReadOnlyGuard(page)
await resetAgentSession(page)

const fails = []

async function ask(q) {
  await page.locator('.composer textarea').fill(q)
  await page.locator('.btn-send').click()
  await page.waitForTimeout(1200)
  for (let i = 0; i < 60; i++) {
    if (await page.locator('.thinking').count() === 0) break
    await page.waitForTimeout(800)
  }
  await page.waitForTimeout(500)
  const last = page.locator('.turn.agent').last()
  return {
    basis: (await last.locator('.basis').innerText().catch(() => '')).trim(),
    headline: await last.locator('.ans-headline').innerText().catch(() => ''),
    nFacts: await last.locator('.fact').count(),
    text: await last.innerText(),
    el: last,
  }
}

const CASES = [
  ['可调用工具？', '系统信息 · 确定性回答'],
  ['你用的什么技术栈', '系统信息 · 确定性回答'],
  ['你是谁', '系统信息 · 确定性回答'],
  ['今天天气怎么样', '系统信息 · 确定性回答'],
]

for (const [q, expectBadge] of CASES) {
  const r = await ask(q)
  console.log('=' .repeat(84))
  console.log(`问：${q}`)
  console.log(`  徽章: ${r.basis}`)
  console.log(`  结论: ${r.headline}`)
  console.log(`  facts: ${r.nFacts}`)
  if (r.basis !== expectBadge) {
    fails.push(`「${q}」徽章应为「${expectBadge}」，实得「${r.basis}」`)
  }
  if (r.text.includes('**')) fails.push(`「${q}」界面出现 Markdown 星号`)
  if (r.text.includes('无可用工具')) fails.push(`「${q}」出现假话"无可用工具"`)
  if (/可调用工具/.test(q)) {
    if (r.nFacts < 9) fails.push(`能力清单 facts 过少（${r.nFacts}）`)
    await r.el.screenshot({ path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\agent-ability.png' })
  }
  console.log('  ' + r.text.replace(/\s+/g, ' ').slice(0, 200))
}

// 对照：业务问题仍应是"数字已校验"
console.log()
console.log('=' .repeat(84))
const biz = await ask('现在决策阈值是多少？')
console.log(`业务问题徽章: ${biz.basis}（应为「模型作答 · 数字已校验」）`)
if (biz.basis !== '模型作答 · 数字已校验') {
  fails.push(`业务问题徽章异常：${biz.basis}`)
}

console.log()
console.log('=' .repeat(84))
const rep = guard.report()
console.log(`护栏：拦截写 ${rep.blockedWrites} · 允许的只读 POST ${rep.allowedWrites}`
  + ` · 读 ${rep.gets} · 非预期放行 ${rep.unexpectedAllowed.length}`)
if (rep.unexpectedAllowed.length) {
  fails.push(`有非预期写请求放行：${rep.unexpectedAllowed}`)
}
console.log(`控制台错误 ${errors.length} 条`)
errors.slice(0, 5).forEach(e => console.log('  ·', e.slice(0, 130)))
console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  console.log('结论：PASS —— 元问题徽章正确、能力清单完整渲染、无假话、业务问题不受影响')
}

await browser.close()
