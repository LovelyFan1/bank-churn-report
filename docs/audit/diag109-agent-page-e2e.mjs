/**
 * 验证：智能助手对话页端到端（浏览器）。
 *
 * 覆盖：
 *   1. 页面可用性状态正确显示
 *   2. 能力边界面板展示
 *   3. 拒答问题 → guard 标记，无确认按钮
 *   4. 查询问题 → 回答 + 依据可展开
 *   5. 建单请求 → 出现确认按钮，**点击前不落库**
 *   6. 点击取消 → 不落库
 *
 * ⚠ 只读原则：拦截并**拦截掉**对 /api/agent/confirm 的请求，
 *   即验证到"按钮出现且可点"为止，不真的建单 ——
 *   生产库 62 条工单是真实数据。
 */
import { chromium } from 'playwright-core'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

const confirmAttempts = []
await page.route('**/api/agent/confirm', route => {
  confirmAttempts.push(route.request().postData())
  return route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ created: true, order: { id: 9999 } }),
  })
})

const fails = []

await page.goto('http://localhost:5173/assistant', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

// 清掉本地会话缓存，保证从干净状态开始（第十六节新增 localStorage 持久化后，
// 残留会话会让 `.last()` 取到旧内容而误报）
await page.evaluate(() => localStorage.removeItem('agent.session.v1'))
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2400)

// 1) 可用性
const statusTxt = (await page.locator('.agent-status').innerText()).replace(/\s+/g, ' ').trim()
console.log('=' .repeat(88))
console.log('可用性状态:', statusTxt)
console.log('=' .repeat(88))
if (!statusTxt.includes('已连接')) fails.push('未显示已连接状态')

// 2) 能力边界
const capsTxt = (await page.locator('.caps summary').innerText()).replace(/\s+/g, ' ').trim()
console.log('能力边界:', capsTxt)
await page.locator('.caps summary').click()
await page.waitForTimeout(400)
const capsBody = (await page.locator('.caps-body').innerText()).replace(/\s+/g, ' ').trim()
console.log('  展开内容:', capsBody.slice(0, 160), '…')
if (!capsBody.includes('答不了')) fails.push('能力边界未列出答不了的主题')

// 辅助：问一个问题并等回答
// ⚠ 选择器随结构化渲染改版更新：旧的 `.answer` 单一文本块已拆分为
//   `.ans-headline` / `.warn` / `.entities` / `.facts` / `.insights`。
async function ask(q) {
  const ta = page.locator('.composer textarea')
  await ta.fill(q)
  await page.locator('.btn-send').click()
  await page.waitForTimeout(1500)
  for (let i = 0; i < 60; i++) {
    if (await page.locator('.thinking').count() === 0) break
    await page.waitForTimeout(1000)
  }
  await page.waitForTimeout(600)
  const bubbles = page.locator('.turn.agent')
  const n = await bubbles.count()
  const last = bubbles.nth(n - 1)
  // 取整段可读文本，兼容两种渲染形态
  const answer = (await last.innerText()).trim()
  return {
    basis: (await last.locator('.basis').innerText()).trim(),
    answer,
    headline: await last.locator('.ans-headline').innerText().catch(() => ''),
    hasPending: await last.locator('.pending').count() > 0,
    hasEvidence: await last.locator('.evidence').count() > 0,
    hasVerifyWarn: await last.locator('.verify-warn').count() > 0,
    el: last,
  }
}

// 3) 拒答
console.log()
console.log('=' .repeat(88))
console.log('问：明年的流失趋势怎样')
const r1 = await ask('明年的流失趋势怎样')
console.log('  口径:', r1.basis)
console.log('  回答:', r1.answer.slice(0, 140).replace(/\n/g, ' '), '…')
console.log('  有确认按钮:', r1.hasPending)
if (!r1.basis.includes('边界')) fails.push('拒答未显示边界标记')
if (r1.hasPending) fails.push('拒答竟然出现确认按钮')

// 4) 查询 + 依据
console.log()
console.log('=' .repeat(88))
console.log('问：C034525 这个人要不要打电话？')
const r2 = await ask('C034525 这个人要不要打电话？')
console.log('  口径:', r2.basis)
console.log('  有依据折叠:', r2.hasEvidence)
console.log('  回答:', r2.answer.slice(0, 200).replace(/\n/g, ' '), '…')
if (!r2.hasEvidence) fails.push('回答未提供依据折叠块')
if (r2.answer.length < 30) fails.push('回答过短')

// 展开依据，看是否列出工具调用
await r2.el.locator('.ev-toggle').click()
await page.waitForTimeout(400)
const evBody = (await r2.el.locator('.ev-body').innerText()).replace(/\s+/g, ' ').trim()
console.log('  依据内容:', evBody.slice(0, 140))
if (!evBody.includes('get_customer_risk')) fails.push('依据未列出实际调用的工具')

// 5) 建单请求 → 确认按钮
console.log()
console.log('=' .repeat(88))
console.log('问：帮我给 C034525 建个挽留工单，负责人写张思远')
const r3 = await ask('帮我给 C034525 建个挽留工单，负责人写张思远')
console.log('  口径:', r3.basis)
console.log('  有确认按钮:', r3.hasPending)
console.log('  回答:', r3.answer.slice(0, 200).replace(/\n/g, ' '), '…')
if (!r3.hasPending) fails.push('建单请求未出现确认按钮')
if (!r3.basis.includes('待确认')) fails.push('建单口径标记不是待确认')
if (confirmAttempts.length !== 0) fails.push('点击前就发出了 confirm 请求（严重）')

await page.screenshot({
  path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\agent-page.png',
})

// 6) 点取消 → 不落库
if (r3.hasPending) {
  await r3.el.locator('.btn-cancel').click()
  await page.waitForTimeout(800)
  const cancelled = await r3.el.locator('.muted').count() > 0
  console.log()
  console.log('  点取消后显示已取消:', cancelled)
  console.log('  期间发出的 confirm 请求数:', confirmAttempts.length, '（应为 0）')
  if (!cancelled) fails.push('取消后未提示')
  if (confirmAttempts.length !== 0) fails.push('取消却发出了 confirm 请求')
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
  console.log('结论：PASS —— 对话、拒答、依据、建单待确认、取消不落库 全部通过')
}

await browser.close()
