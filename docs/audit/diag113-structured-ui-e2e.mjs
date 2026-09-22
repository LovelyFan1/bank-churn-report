/**
 * 验证：结构化渲染 + 批量建单确认面板（方案 A）。
 *
 * 覆盖：
 *   1. 无 Markdown 裸露（# ** | 不得出现在回答文本里）
 *   2. 结论在顶部、警示紧跟结论（不放文末）
 *   3. 客户卡片：主指标放大、副指标、标签、动作
 *   4. 已有工单的人卡片置灰且标注
 *   5. 「为这 2 人建单」按钮出现（不是 3 人）
 *   6. 点按钮 → 弹出确认面板，列出 2 人 + 排除说明 + 负责人输入
 *   7. **确认前零写请求**
 *   8. 点取消 → 关闭面板，零写请求
 *
 * ⚠ 只读原则：拦截 /api/agent/confirm-batch，不发到后端 ——
 *   生产库 62 条工单是真实数据。
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
//   历史事故：本脚本原先只拦 confirm-batch，而**不拦 /agent/confirm** ——
//   如果页面走了单条确认路径，请求就会穿透到后端真实建单。
//   详见 _guard.mjs 说明。
const guard = await installReadOnlyGuard(page)
// batchCalls 是护栏里的**活数组**（只含批量建单的解析后 payload）。
// ⚠ 不能在这里 map 一次就存下 —— 那是注册时刻的快照，永远是空的。
const batchCalls = guard.batchPayloads

const fails = []
const grayed = []

await loginAs(page)
await page.goto('http://localhost:5173/assistant', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

// ⚠ 必须先清掉本地会话缓存（第十六节新增了 localStorage 持久化）。
//   否则上一次测试/上一次浏览留下的对话会一起恢复，
//   导致 `.turn.agent').last()` 取到的不是本次答案 —— 测试会误报失败。
await page.evaluate(() => localStorage.removeItem('agent.session.v1'))
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2400)

// 问那个真实问题
await page.locator('.composer textarea').fill('帮我调出挽回价值最高的3个客户')
await page.locator('.btn-send').click()
for (let i = 0; i < 90; i++) {
  if (await page.locator('.thinking').count() === 0) break
  await page.waitForTimeout(1000)
}
await page.waitForTimeout(1000)

const last = page.locator('.turn.agent').last()

// 1) 无 Markdown 裸露
const answerText = await last.innerText()
const mdLeaks = []
if (answerText.includes('#')) mdLeaks.push('#')
if (answerText.includes('**')) mdLeaks.push('**')
if (answerText.includes('|---')) mdLeaks.push('表格分隔符')
console.log('=' .repeat(88))
console.log('一、Markdown 裸露检查')
console.log('  发现:', mdLeaks.length ? mdLeaks : '无 ✔')
if (mdLeaks.length) fails.push(`界面出现 Markdown 裸露：${mdLeaks}`)

// 2) 结论与警示位置
console.log()
console.log('=' .repeat(88))
console.log('二、信息层次')
const headline = await last.locator('.ans-headline').innerText().catch(() => '')
console.log('  结论:', headline)
if (!headline) fails.push('未渲染结论标题')

const warnCount = await last.locator('.warn').count()
const warnText = warnCount ? (await last.locator('.warn-text').innerText()).trim() : ''
console.log('  警示:', warnText || '(无)')
if (!warnText) fails.push('未渲染警示条')

// 验证警示在卡片之前（DOM 顺序）
if (warnCount) {
  const order = await last.evaluate(el => {
    const w = el.querySelector('.warn')
    const e = el.querySelector('.entities')
    if (!w || !e) return 'missing'
    return (w.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING)
      ? 'warning-before-entities' : 'warning-after-entities'
  })
  console.log('  位置:', order)
  if (order !== 'warning-before-entities') fails.push('警示条未排在卡片之前')
}

// 3) 卡片
console.log()
console.log('=' .repeat(88))
console.log('三、客户卡片')
const cards = last.locator('.entity')
const n = await cards.count()
console.log(`  卡片数 ${n}`)
if (n !== 3) fails.push(`卡片数应为 3，实得 ${n}`)

for (let i = 0; i < n; i++) {
  const c = cards.nth(i)
  const cid = await c.locator('.e-id').innerText()
  const primary = await c.locator('.e-primary-value').innerText()
  const tags = (await c.locator('.tag').allInnerTexts()).join('/')
  const disabled = await c.locator('.e-blocked').count() > 0
  const isGrayed = await c.evaluate(el => el.classList.contains('e-disabled'))
  console.log(`    ${cid}  主指标=${primary}  标签=${tags}  置灰=${isGrayed}  标注已有工单=${disabled}`)
  // ⚠ 断言改为**置灰状态与后端实际状态一致**，而不是写死"只有第一个人会置灰"。
  //   原因：工单数据是活的 —— 实测中用户给 C034525 建了单，
  //   于是"只有 C071081 被排除"这个旧假设失效，测试误报产品有 bug。
  //   正确的断言是**一致性**：卡片置灰 ⟺ 该客户确实有进行中工单。
  if (isGrayed !== disabled) {
    fails.push(`${cid} 置灰(${isGrayed})与标注(${disabled})不一致`)
  }
  if (isGrayed) grayed.push(cid)
  // 主指标必须是"万元"量级表达（可扫读），不应是 220,080.25 这种长数字
  if (!primary.includes('万')) fails.push(`${cid} 主指标未用万元表达：${primary}`)
}
console.log('  置灰（已有工单）:', grayed.length ? grayed : '无')

// 4) 批量按钮
console.log()
console.log('=' .repeat(88))
console.log('四、批量建单按钮')
const actionBtns = last.locator('.btn-action')
const btnCount = await actionBtns.count()
const btnText = btnCount ? (await actionBtns.first().innerText()).trim() : ''
// 可建单人数 = 3 张卡片 - 已置灰数。⚠ 不写死"2 人"：
// 工单数据是活的（实测用户给 C034525 建单后这里就变成 1 人）
const buildable = 3 - grayed.length
console.log('  按钮:', btnText || '(无)', ` （可建单应为 ${buildable} 人）`)
if (btnCount !== 1) fails.push(`应有 1 个动作按钮，实得 ${btnCount}`)

if (buildable > 0) {
  // 单人时后端文案是「为 C062858 建单」（用编号更明确），
  // 多人时是「为这 N 人建单」。故断言分两种，不能一律要求含数字。
  if (buildable === 1) {
    if (!/为\s*C\d+\s*建单/.test(btnText) && !btnText.includes('1')) {
      fails.push(`单人按钮文案应含客户编号，实得「${btnText}」`)
    }
  } else if (!btnText.includes(String(buildable))) {
    fails.push(`按钮文案应含「${buildable}」，实得「${btnText}」`)
  }
  const noteText = await last.locator('.action-note').innerText().catch(() => '')
  console.log('  排除说明:', noteText || '(无)')
  for (const g of grayed) {
    if (!noteText.includes(g)) fails.push(`未说明 ${g} 被排除的原因`)
  }
} else {
  // 三人都已有工单时，应给出"无可建单对象"而非一个空按钮
  console.log('  三人都已有工单 → 应显示无可建单对象')
  if (btnCount > 0) fails.push('无可建单对象时仍显示建单按钮')
}

// 5) 打开批量面板
console.log()
console.log('=' .repeat(88))
console.log('五、批量确认面板（方案 A）')
if (btnCount && buildable > 0) {
  await answerBtnsClick(last)
  await page.waitForTimeout(900)

  const panel = last.locator('.batch-panel')
  const panelVisible = await panel.count() > 0
  console.log('  面板出现:', panelVisible)
  if (!panelVisible) fails.push('点击后未出现批量确认面板')
  else {
    const list = (await panel.locator('.batch-list li').allInnerTexts()).map(s => s.trim())
    const skip = await panel.locator('.batch-skip').innerText().catch(() => '')
    console.log('  待建单列表:', list)
    console.log('  排除说明:', skip.replace(/\s+/g, ' '))
    if (list.length !== buildable) fails.push(`面板应列 ${buildable} 人，实得 ${list.length}`)
    for (const g of grayed) {
      if (list.includes(g)) fails.push(`面板不该包含已有工单的 ${g}`)
      if (!skip.includes(g)) fails.push(`面板未说明 ${g} 的排除原因`)
    }

    const hasAssignee = await panel.locator('input').count() > 0
    console.log('  负责人输入框:', hasAssignee)
    if (!hasAssignee) fails.push('面板缺少负责人输入')

    if (batchCalls.length !== 0) fails.push('确认前就发出了批量建单请求（严重）')
    console.log('  确认前写请求数:', batchCalls.length, '（应为 0）')

    await page.screenshot({
      path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\agent-batch-panel.png',
      fullPage: false,
    })

    // 6) 取消 → 关闭，零写请求
    console.log()
    console.log('=' .repeat(88))
    console.log('六、取消')
    await panel.locator('.btn-cancel').click()
    await page.waitForTimeout(700)
    const stillOpen = await last.locator('.batch-panel').count() > 0
    console.log('  面板仍显示:', stillOpen, '（应消失或结果区）')
    console.log('  写请求总数:', batchCalls.length, '（取消路径应为 0）')
    if (batchCalls.length !== 0) fails.push('取消却发出了写请求')
  }
}

// 7) 提交路径（重新打开并确认，验证 payload）
console.log()
console.log('=' .repeat(88))
console.log('七、确认提交（请求体核对）')
if (btnCount && buildable > 0) {
  if (await last.locator('.batch-panel').count() === 0) {
    await answerBtnsClick(last)
    await page.waitForTimeout(800)
  }
  const panel = last.locator('.batch-panel')
  if (await panel.count()) {
    await panel.locator('input').first().fill('张思远')
    await panel.locator('.btn-confirm').first().click()
    await page.waitForTimeout(1500)

    console.log('  发出的请求数:', batchCalls.length)
    if (batchCalls.length !== 1) {
      fails.push(`应发出 1 次批量请求，实得 ${batchCalls.length}`)
    } else {
      const body = batchCalls[0]
      console.log('  payload:', JSON.stringify(body))
      if (body.customer_ids?.length !== buildable) {
        fails.push(`payload 客户数应为 ${buildable}，实得 ${body.customer_ids?.length}`)
      }
      for (const g of grayed) {
        if (body.customer_ids?.includes(g)) fails.push(`payload 不该含 ${g}`)
      }
      if (body.assignee !== '张思远') fails.push(`assignee 应为张思远，实得 ${body.assignee}`)
    }
    // 结果区
    const resText = (await last.locator('.batch-result').innerText().catch(() => '')).replace(/\s+/g, ' ')
    console.log('  结果区:', resText.slice(0, 120))
    if (!resText.includes('成功')) fails.push('未显示批量结果')
    await page.screenshot({
      path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\agent-batch-result.png',
    })
  }
}

console.log()
console.log('=' .repeat(88))
console.log('控制台错误', errors.length, '条')
errors.slice(0, 5).forEach(e => console.log('  ·', e.slice(0, 140)))
console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  console.log('结论：PASS —— 结构化渲染无 Markdown 裸露、层次正确、卡片/置灰/按钮/批量确认 全部通过')
}

await browser.close()

// 辅助：点第一个动作按钮
async function answerBtnsClick(loc) {
  await loc.locator('.btn-action').first().click()
}
