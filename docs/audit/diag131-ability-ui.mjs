/**
 * 前端核验：能力范围面板只列 9 个可做项，不含"答不了"。
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

console.log('=' .repeat(84))
console.log('一、能力范围面板（页面顶部 <details>）')
console.log('=' .repeat(84))
const summary = (await page.locator('.caps summary').innerText()).replace(/\s+/g, ' ').trim()
console.log('  折叠标题:', summary)
if (summary.includes('答不了')) fails.push('折叠标题仍提「答不了」')
if (!summary.includes('9')) fails.push(`折叠标题未显示 9 个可查项：${summary}`)

await page.locator('.caps summary').click()
await page.waitForTimeout(500)

const items = await page.locator('.caps-col li').allInnerTexts()
console.log(`  列表项数: ${items.length}`)
items.forEach((s, i) => console.log(`    ${i + 1}. ${s.replace(/\s+/g, ' ')}`))
if (items.length !== 9) fails.push(`能力列表应为 9 项，实得 ${items.length}`)

const capsText = await page.locator('.caps').innerText()
const banned = ['答不了', '数据不支持', 'retention_success_rate', 'temporal_trend',
                'strategy_comparison', '样本仅 1 组', '无真实回访数据']
for (const b of banned) {
  if (capsText.includes(b)) fails.push(`能力面板仍出现「${b}」`)
}
console.log(`  含"答不了"字样: ${capsText.includes('答不了')}`)

// 9 个工具名都应出现
const toolNames = [
  'get_customer_risk', 'list_customers', 'get_model_thresholds',
  'get_business_summary', 'get_workorder_stats', 'list_work_orders',
  'propose_create_work_order', 'propose_update_work_order',
  'propose_delete_work_order',
]
const miss = toolNames.filter(n => !capsText.includes(n))
if (miss.length) fails.push(`面板缺工具：${miss}`)
console.log(`  9 个工具名齐全: ${miss.length === 0}`)

// "需确认" 标记应出现在 3 个写工具上
const nTag = (capsText.match(/需确认/g) || []).length
console.log(`  「需确认」标记数: ${nTag}（应为 3）`)
if (nTag !== 3) fails.push(`「需确认」标记应为 3，实得 ${nTag}`)

await page.locator('.caps').screenshot({
  path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\agent-caps.png',
})

// ── 二、对话中的能力回答 ──
console.log()
console.log('=' .repeat(84))
console.log('二、问「可调用工具？」的回答')
console.log('=' .repeat(84))
await page.locator('.composer textarea').fill('可调用工具？')
await page.locator('.btn-send').click()
for (let i = 0; i < 60; i++) {
  if (await page.locator('.thinking').count() === 0) break
  await page.waitForTimeout(800)
}
await page.waitForTimeout(600)
const last = page.locator('.turn.agent').last()
const body = await last.innerText()
console.log('  口径:', (await last.locator('.basis').innerText()).trim())
console.log('  结论:', await last.locator('.ans-headline').innerText())
console.log('  facts 数:', await last.locator('.fact').count())
if (body.includes('答不了')) fails.push('对话答案仍提「答不了」')
if (body.includes('**')) fails.push('界面出现 Markdown 星号')
const nFacts = await last.locator('.fact').count()
if (nFacts !== 9) fails.push(`对话 facts 应为 9，实得 ${nFacts}`)
await last.screenshot({
  path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\agent-ability2.png',
})

console.log()
console.log('=' .repeat(84))
console.log(`控制台错误 ${errors.length} 条`)
errors.slice(0, 5).forEach(e => console.log('  ·', e.slice(0, 130)))
const rep = guard.report()
console.log(`护栏：拦截写 ${rep.blockedWrites} · 允许的只读 POST ${rep.allowedWrites}`
  + ` · 非预期放行 ${rep.unexpectedAllowed.length}`)
console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  console.log('结论：PASS —— 能力面板与对话答案都只列 9 个可做项，无「答不了」')
}

await browser.close()
