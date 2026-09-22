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
import { installReadOnlyGuard, loginAs } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1100 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

// ⚠ 用共享只读护栏（唯一一条兜底路由，内部按 URL/method 分发）。
//   历史事故：本脚本原先自己注册多条 page.route，导致 confirm 请求
//   穿透到后端，把用户手工创建的工作单 #63 真实删除了。
//   详见 docs/audit/_guard.mjs 顶部说明。
const guard = await installReadOnlyGuard(page)
const writes = guard.blocked   // 兼容下方既有断言：被拦下的写请求

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

await loginAs(page)
await page.goto('http://localhost:5173/assistant', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

// 先清掉可能存在的旧缓存，保证从干净状态开始
await page.evaluate(() => localStorage.removeItem('agent.session.v1.liming'))
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2200)

console.log('=' .repeat(88))
console.log('一、发两轮对话')
await ask('现在决策阈值是多少？')
await ask('工单处理得怎么样？')
let turns = await page.locator('.turn').count()
console.log('  turns 数:', turns)
if (turns < 4) fails.push(`应至少 4 条消息（2问2答），实得 ${turns}`)

const lsRaw = await page.evaluate(() => localStorage.getItem('agent.session.v1.liming'))
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
// ⚠ 前置夹具：必须**先自己造一张 pending 工单**，不能依赖"库里恰好有"。
//   实测教训：本脚本原先假设库里存在待处理工单，而那张单被之前的测试
//   事故删掉了（详见 _guard.mjs）。于是"未出现取消按钮"被误报成产品缺陷，
//   实际上 Agent 回答「当前无待处理工单，无需取消」是**正确的**。
//
// ⚠ 夹具必须用 page.request 发（实测 diag125：page.request **绕过**
//   page.route，护栏拦不住它），因此**必须自己负责删除**。
//   这是唯一允许真实写库的地方，且严格限定在 finally 里清理。
//
// ⚠ 引入鉴权后必须**显式带令牌**（本次修复）：
//   page.request 是独立的请求上下文，**不共享** page.evaluate 写入的
//   localStorage —— 它不会自动带上 Authorization 头。
//   实测症状：夹具建单返回 401「未登录或缺少令牌」→ 夹具不存在 →
//   「取消工单未出现确认面板」等 4 项断言失败，看起来像产品坏了，
//   实际是测试没登录。
console.log()
console.log('=' .repeat(88))
console.log('五、造夹具：新建一张处于 pending 的工单')
const API = 'http://localhost:8000/api'
// 与 loginAs 同源的令牌（从页面 localStorage 取，确保是真实登录过的会话）
const AUTH_HEADERS = await page.evaluate(() => {
  const t = localStorage.getItem('auth.token.v1')
  return t ? { Authorization: 'Bearer ' + t } : {}
})
if (!AUTH_HEADERS.Authorization) {
  fails.push('未取得登录令牌，夹具无法创建（请确认已调用 loginAs）')
}
let fixtureOrderId = null
let fixtureCreated = false    // 只有本测试**新建**的才删；复用的不动
{
  const r = await page.request.post(`${API}/work-orders`, {
    headers: AUTH_HEADERS,
    data: {
      customer_id: 'C062858', customer_name: 'Pirogov', geography: 'France',
      risk_level: 'CRITICAL', probability: 0.8883, balance: 225534.51,
      risk_factors: [], strategy: '客户经理上门 + 定制挽留方案',
      assignee: '', channel: 'relationship', value_tier_snapshot: 'HIGH',
      expected_value_snapshot: 200331.86,
    },
  })
  if (r.status() === 201) {
    fixtureOrderId = (await r.json()).id
    fixtureCreated = true
    console.log('  已建夹具工单 #' + fixtureOrderId)
  } else {
    const body = await r.text()
    console.log('  建夹具返回 ' + r.status() + '：' + body.slice(0, 130))
    const m = body.match(/#(\d+)/)
    if (m) {
      fixtureOrderId = Number(m[1])
      console.log('  复用已有进行中工单 #' + fixtureOrderId + '（不删，非本测试创建）')
    } else {
      fails.push('无法准备夹具工单，取消流程无法验证')
    }
  }
}

try {
console.log()
console.log('=' .repeat(88))
console.log('五之二、问「帮我取消已建单待处理状态客户」')
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
// ⚠ 断言必须用**本次夹具的实际编号**，不能硬编码 '63'。
//   实测问题：work_orders.id 是 INTEGER PRIMARY KEY（无 AUTOINCREMENT），
//   插入时取 max(id)+1 —— 删掉旧夹具后 id 会被**复用**。原先硬编码 '63'，
//   当夹具落到 #62 时断言必然失败，而产品行为其实完全正确
//   （结论里明明写着「取消（删除）工单 #62」）。这是测试的假告警。
if (fixtureOrderId && !headline.includes(String(fixtureOrderId))) {
  fails.push(`结论未点明工单编号（期望 #${fixtureOrderId}）：${headline}`)
} else if (!fixtureOrderId && !/#\d+/.test(headline)) {
  fails.push(`结论未点明工单编号：${headline}`)
}

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
const lsAfter = await page.evaluate(() => localStorage.getItem('agent.session.v1.liming'))
console.log('  清空后 turns 数:', turns)
console.log('  localStorage:', lsAfter === null ? '已清除' : `仍有 ${lsAfter.length} 字节`)
if (turns !== 0) fails.push(`清空后仍有 ${turns} 条消息`)
if (lsAfter !== null) fails.push('清空后 localStorage 未被清除')

} finally {
  // ── 夹具清理（必须无条件的）───────────────────────────
  // ⚠ 夹具是用 page.request 真实写库的（绕过护栏，见上面说明），
  //   所以这里必须删掉。放在 finally：即使断言失败/抛异常也要清理，
  //   否则测试会在库里留垃圾 —— 之前的事故正是"以为清干净了"。
  console.log()
  console.log('=' .repeat(88))
  console.log('七、清理夹具')
  if (fixtureOrderId && fixtureCreated) {
    const del = await page.request.delete(`${API}/work-orders/${fixtureOrderId}`,
      { headers: AUTH_HEADERS })
    console.log(`  删除夹具工单 #${fixtureOrderId} → HTTP ${del.status()}`)
    if (del.status() !== 204) fails.push('夹具清理失败，库中残留测试工单')
  } else if (fixtureOrderId) {
    console.log(`  工单 #${fixtureOrderId} 非本测试创建，保留不动`)
  } else {
    console.log('  （无夹具需要清理）')
  }
}

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
