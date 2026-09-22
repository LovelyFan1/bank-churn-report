/**
 * 验证：会话上下文（追问）——前端是否真的把 context 传下去。
 *
 * 覆盖：
 *   1. 第一轮查 Top3 → localStorage 里出现 context（3 个编号）
 *   2. 追问「第二个为什么值得投入」→ 请求体带上了 context，且答案指向第二个人
 *   3. 追问「他的余额是多少」→ 仍指向同一个人
 *   4. 「给他建个工单」→ 出现待确认按钮，**不点**（零写入）
 *   5. 刷新页面 → context 仍在，追问仍成立
 *   6. 清空会话 → context 一并清空（不留状态残留）
 *   7. 徽章未退化（仍为"数字已校验"，无未溯源数字）
 *
 * ⚠ 只读：用共享护栏拦下所有写请求，建单只验证按钮出现，绝不点确认。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, loginAs } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1100 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

const guard = await installReadOnlyGuard(page)

// 记录每次 /agent/ask 的请求体，用于确认 context 真的被回传
const askBodies = []
page.on('request', req => {
  if (req.url().includes('/api/agent/ask') && req.method() === 'POST') {
    try { askBodies.push(JSON.parse(req.postData() || '{}')) } catch (_) {}
  }
})

const fails = []
const ok = (cond, msg) => {
  console.log(`  [${cond ? 'OK ' : 'FAIL'}] ${msg}`)
  if (!cond) fails.push(msg)
}

async function ask(q) {
  await page.locator('.composer textarea').fill(q)
  await page.locator('.btn-send').click()
  await page.waitForTimeout(1200)
  for (let i = 0; i < 90; i++) {
    if (await page.locator('.thinking').count() === 0) break
    await page.waitForTimeout(1000)
  }
  await page.waitForTimeout(800)
}

const readCtx = () => page.evaluate(() => {
  try {
    const o = JSON.parse(localStorage.getItem('agent.session.v1.liming') || '{}')
    return o.context || null
  } catch (_) { return null }
})

await loginAs(page)
await page.goto('http://localhost:5173/assistant', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
await page.evaluate(() => localStorage.removeItem('agent.session.v1.liming'))
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2200)

console.log('='.repeat(90))
console.log('一、第一轮：查 Top3 → context 应出现 3 个编号')
await ask('帮我调出挽回价值最高的3个客户')
let c1 = await readCtx()
const ids1 = (c1?.customers || []).map(c => c.id)
console.log('  context.customers =', JSON.stringify(ids1))
ok(ids1.length === 3, `第一轮后 context 有 3 个客户（实际 ${ids1.length}）`)
ok(askBodies[0] && 'context' in askBodies[0],
   '第一轮请求体带 context 字段（首轮为空对象）')

console.log('\n二、追问「第二个为什么值得投入」→ 应解析到第 2 个人')
await ask('第二个为什么值得投入')
const b2 = askBodies[1] || {}
console.log('  第2轮请求 context =', JSON.stringify((b2.context?.customers || []).map(c => c.id)))
ok((b2.context?.customers || []).length === 3,
   '第2轮请求**带上了**上一轮的 context（这是追问能成立的前提）')
const head2 = (await page.locator('.turn').last().innerText()).slice(0, 120)
console.log('  第2轮回答:', head2.replace(/\n+/g, ' | '))
ok(head2.includes(ids1[1]), `回答指向第 2 个人 ${ids1[1]}`)

console.log('\n三、追问「他的余额是多少」→ 仍指向同一个人')
await ask('他的余额是多少')
const head3 = (await page.locator('.turn').last().innerText()).slice(0, 120)
console.log('  第3轮回答:', head3.replace(/\n+/g, ' | '))
ok(head3.includes(ids1[1]), `回答仍指向 ${ids1[1]}`)

console.log('\n四、「给他建个工单」→ 出现待确认按钮（不点）')
await ask('给他建个工单')
const lastTurn = page.locator('.turn').last()
const hasPending = await lastTurn.locator('.confirm-actions, .pending-actions, button').count()
const turnText = await lastTurn.innerText()
console.log('  含"待确认/确认"字样:', /待确认|确认/.test(turnText))
ok(/待确认|确认/.test(turnText), '写操作转为待确认（未直接执行）')
const writesBefore = guard.blocked.length + guard.allowedWrites.length
console.log('  到此为止的写请求数:', writesBefore)

console.log('\n五、徽章未退化（记忆不得让校验失效）')
const badges = await page.locator('.basis, .badge').allInnerTexts()
const bad = badges.filter(t => /未溯源|无法溯源/.test(t))
console.log('  徽章:', JSON.stringify(badges.slice(-4)))
ok(bad.length === 0, `无"未溯源"徽章（实际 ${bad.length} 条）`)

console.log('\n六、刷新页面 → context 仍应恢复')
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
const c2 = await readCtx()
const ids2 = (c2?.customers || []).map(c => c.id)
console.log('  刷新后 context =', JSON.stringify(ids2))
ok(ids2.length === 3, `刷新后 context 保留 3 个客户（实际 ${ids2.length}）`)

console.log('\n七、清空会话 → context 应一并清空')
await page.locator('button:has-text("清空")').first().click()
await page.waitForTimeout(1200)
const c3 = await readCtx()
const stored = await page.evaluate(() => localStorage.getItem('agent.session.v1.liming'))
console.log('  清空后 localStorage:', stored === null ? '已删除' : stored.slice(0, 60))
ok(stored === null, '清空后 localStorage 键被删除（无残留）')

console.log('\n' + '='.repeat(90))
console.log('控制台错误', errors.length, '条')
if (errors.length) errors.slice(0, 5).forEach(e => console.log('   !', e.slice(0, 160)))

console.log('\n写请求总览：')
console.log('  被拦截:', guard.blocked.length, '  被放行:', guard.allowedWrites.length)
const unexpected = guard.allowedWrites.filter(w => !w.includes('/api/agent/ask'))
ok(unexpected.length === 0, `无预期外的写请求（实际 ${unexpected.length}）`)

console.log('\n结论：' + (fails.length === 0 && errors.length === 0
  ? 'PASS —— 追问链成立、context 随请求流转、刷新保留、清空彻底、零写入'
  : `FAIL —— ${fails.length} 项未通过`))
if (fails.length) fails.forEach(f => console.log('   × ' + f))

await browser.close()
