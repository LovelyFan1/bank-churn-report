/**
 * 验证：会话本地缓存 + 取消工单按钮。
 *
 * 覆盖：
 *   1. 发几轮对话 → 刷新页面 → 会话仍在（localStorage 恢复）
 *   2. 恢复后显示"已从本地缓存恢复"提示
 *   3. 恢复后交互态被重置（不出现卡住的"执行中…"）
 *   4. 点「清空会话」→ 内容清空且 localStorage 被清
 *   5. 问「取消待处理工单」→ 出现确认按钮（且点前零写请求）
 *
 * ⚠ 只读：拦截所有写请求，不真的删单（用户手工建的 #63 必须保留）。
 */
import { chromium } from 'playwright-core'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1100 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

const writes = []
await page.route('**/api/agent/confirm', route => {
  writes.push(route.request().postData())
  return route.fulfill({ status: 200, contentType: 'application/json',
                         body: JSON.stringify({ created: false, deleted: true, order_id: 9999 }) })
})
await page.route('**/api/agent/confirm-batch', route => {
  writes.push(route.request().postData())
  return route.fulfill({ status: 200, contentType: 'application/json',
                         body: JSON.stringify({ requested: 0, succeeded: 0, failed_count: 0,
                                                skipped_count: 0, created: [], failed: [], skipped: [] }) })
})

const fails = []

async function ask(q) {
  await page.locator('.composer textarea').fill(q)
  await page.locator('.btn-send').click()
  await page.waitForTimeout(1200)
  for (let i = 0; i < 90; i++) {
    if (await page.locator('.thinking').count() === 0) break
    await page.waitForTimeout(1000)
  }
  await page.waitForTimeout(700)
}

await page.goto('http://localhost:5173/assistant', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

// 先清掉可能存在的旧缓存，保证从干净状态开始
await page.evaluate(() => localStorage.removeItem('agent.session.v1'))
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2200)

console.log('=' .repeat(88))
console.log('一、发两轮对话')
await ask('现在决策阈值是多少？')
await ask('工单处理得怎么样？')
let turns = await page.locator('.turn').count()
console.log('  turns 数:', turns)
if (turns < 4) fails.push(`应至少 4 条消息（2问2答），实得 ${turns}`)

const lsRaw = await page.evaluate(() => localStorage.getItem('agent.session.v1'))
console.log('  localStorage 已写入:', !!lsRaw, `(${lsRaw ? lsRaw.length : 0} 字节)`)
if (!lsRaw) fails.push('会话未写入 localStorage')

// ── 2) 刷新后仍存在 ────────────────────────────────
console.log()
console.log('=' .repeat(88))
console.log('二、刷新页面 → 会话应恢复')
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2600)

turns = await page.locator('.turn').count()
const tip = await page.locator('.restored-tip').count()
const tipText = tip ? (await page.locator('.restored-tip').innerText()).replace(/\s+/g, ' ') : ''
console.log('  刷新后 turns 数:', turns)
console.log('  恢复提示:', tipText || '(无)')
if (turns < 4) fails.push(`刷新后会话丢失（turns=${turns}）`)
if (!tip) fails.push('未显示"已从本地缓存恢复"提示')

// ── 3) 交互态被重置 ────────────────────────────────
console.log()
console.log('=' .repeat(88))
console.log('三、恢复后不应有卡住的进行中态')
const stuck = await page.locator('.btn-confirm:disabled, .spinner').count()
const thinking = await page.locator('.thinking').count()
console.log('  卡住的按钮/加载中:', stuck, ' thinking:', thinking)
if (thinking > 0) fails.push('恢复后仍显示"正在查询"')
if (stuck > 0) fails.push('恢复后出现卡住的禁用按钮')

// ── 4) 新开标签页也共享（localStorage 特性）────────
console.log()
console.log('=' .repeat(88))
console.log('四、新标签页（模拟"切换窗口"）')
const page2 = await ctx.newPage()
await page2.goto('http://localhost:5173/assistant', { waitUntil: 'networkidle' })
await page2.waitForTimeout(2600)
const turns2 = await page2.locator('.turn').count()
console.log('  新标签页 turns 数:', turns2)
if (turns2 < 4) fails.push(`新标签页会话未恢复（turns=${turns2}）`)
await page2.close()

// ── 5) 取消工单按钮 ────────────────────────────────
console.log()
console.log('=' .repeat(88))
console.log('五、问「帮我取消已建单待处理状态客户」')
await ask('帮我取消已建单待处理状态客户')
const last = page.locator('.turn.agent').last()
const basis = (await last.locator('.basis').innerText()).trim()
const headline = await last.locator('.ans-headline').innerText().catch(() => '')
const hasPending = await last.locator('.pending').count() > 0
const confirmBtn = await last.locator('.btn-confirm').count()
console.log('  口径:', basis)
console.log('  结论:', headline)
console.log('  有确认区:', hasPending, ' 确认按钮数:', confirmBtn)
console.log('  写请求数:', writes.length, '（点击前应为 0）')

if (writes.length !== 0) fails.push('点击前就发出了写请求')
if (!hasPending) fails.push('取消工单未出现确认面板（用户看不到按钮）')
if (!headline.includes('63')) fails.push(`结论未点明工单编号：${headline}`)

await last.screenshot({ path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\agent-cancel.png' })

// ── 5b) 取消工单的确认区 ────────────────────────────────
// ⚠ 取消工单走的是**单条 pending 确认区**（Agent 已明确提议删哪一张），
//   而不是 workorder_list 的"列表 + 批量动作"形态 —— 因为模型先
//   list_work_orders 拿到 id 再 propose_delete_work_order，
//   到前端时已是确定的一单。故这里断言的是 .pending 内的按钮文案。
console.log()
console.log('=' .repeat(88))
console.log('五之二、取消确认区（单条）')
const cancelConfirmBtn = last.locator('.pending .btn-confirm')
const cCnt = await cancelConfirmBtn.count()
console.log('  pending 确认按钮数:', cCnt)
if (!cCnt) fails.push('未出现取消确认按钮')

if (cCnt) {
  const label = (await cancelConfirmBtn.first().innerText()).trim()
  const warnText = await last.locator('.warn-text').innerText().catch(() => '')
  const bodyText = await last.innerText()
  console.log('  按钮文案:', label)
  console.log('  警示:', warnText.replace(/\s+/g, ' '))
  // ⚠ 关键断言：文案必须是"取消"而不是"建单"。
  //   此前按钮一律写「确认建单」，在删除场景会严重误导用户。
  if (!label.includes('取消')) fails.push(`按钮文案应含「取消」，实得「${label}」`)
  if (label.includes('建单')) fails.push('取消场景的按钮不该出现「建单」字样')
  if (!warnText.includes('不可恢复')) fails.push('未提示删除不可逆')
  // Markdown 星号不得露出（历史上踩过两次）
  if (bodyText.includes('**')) fails.push('界面出现 Markdown 星号')
  if (writes.length !== 0) fails.push('打开确认区就发了写请求（严重）')
  console.log('  写请求数:', writes.length, '（应为 0）')
  await last.screenshot({ path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\agent-cancel.png' })
}

// ── 6) 清空会话 ────────────────────────────────────
console.log()
console.log('=' .repeat(88))
console.log('六、清空会话')
await page.locator('.btn-clear').click()
await page.waitForTimeout(800)
turns = await page.locator('.turn').count()
const lsAfter = await page.evaluate(() => localStorage.getItem('agent.session.v1'))
console.log('  清空后 turns 数:', turns)
console.log('  localStorage:', lsAfter === null ? '已清除' : `仍有 ${lsAfter.length} 字节`)
if (turns !== 0) fails.push(`清空后仍有 ${turns} 条消息`)
if (lsAfter !== null) fails.push('清空后 localStorage 未被清除')

console.log()
console.log('=' .repeat(88))
console.log('控制台错误', errors.length, '条')
errors.slice(0, 6).forEach(e => console.log('  ·', e.slice(0, 140)))
console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  console.log('结论：PASS —— 会话本地缓存（刷新/新标签页均恢复）、清空生效、取消工单按钮出现、零写入')
}

await browser.close()
