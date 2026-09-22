/**
 * 验证：首页「优先干预 Top10」的建单弹窗也能预填建议理由。
 *
 * 若只给客户管理页加、首页不加，就又是"同一件事两个行为" —— 本项目
 * 已经因为这类不一致改过 6 处。故两处都必须验。
 *
 * 只读：拦截所有 POST，不发到后端。
 */
import { chromium } from 'playwright-core'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

const noteReqs = []
await page.route('**/api/**', route => {
  const req = route.request()
  if (req.url().includes('/suggested-note')) noteReqs.push(req.url())
  if (req.method() !== 'GET') {
    return route.fulfill({
      status: 201, contentType: 'application/json',
      body: JSON.stringify({ id: 9999, status: 'pending' }),
    })
  }
  return route.continue()
})

await page.goto('http://localhost:5173/dashboard', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

// Top10 表格里的建单按钮。文案是「创建工单」（不是「建单」）——
// 首版脚本按「建单」找，结果 0 个，是脚本的错不是产品的错。
const btns = page.locator('button', { hasText: /创建工单/ })
const cnt = await btns.count()
console.log(`首页可点建单按钮 ${cnt} 个`)

const fails = []
if (cnt === 0) {
  fails.push('首页找不到建单按钮')
} else {
  await btns.first().click()
  // ⚠ 首页弹窗的类名与客户管理页**不同**：这里是 .modal-card（内层）+
  //   .modal-overlay（外层），客户管理页是 .modal。首版脚本按 .modal 等，
  //   超时失败 —— 又是脚本假设错，不是产品错。
  await page.waitForSelector('.modal-card', { state: 'visible', timeout: 8000 })
  await page.waitForTimeout(2000)

  const note = await page.locator('.modal-card textarea').first().inputValue()
  const assist = await page.locator('.note-assist').count()
  const hint = (await page.locator('.note-assist-hint').allInnerTexts()).join(' | ')
  const err = (await page.locator('.note-assist-err').allInnerTexts()).join(' | ')

  console.log()
  console.log('='.repeat(88))
  console.log(`建议理由请求 ${noteReqs.length} 次`)
  noteReqs.forEach(u => console.log('  ·', u.replace(/^.*\/api/, '/api')))
  console.log('='.repeat(88))
  console.log(`备注长度 ${note.length}`)
  console.log('辅助区:', hint || '(无)')
  if (err) console.log('错误:', err)
  console.log('-'.repeat(88))
  console.log(note || '(空)')

  if (assist === 0) fails.push('首页弹窗没有建议理由辅助区')
  if (!note.trim()) fails.push('首页弹窗备注未被预填')
  if (note && !note.includes('口径')) fails.push('备注缺口径标注')
  if (note && note.includes('**')) fails.push('备注含 Markdown 星号')
  if (err) fails.push('生成失败: ' + err)
  if (noteReqs.length === 0) fails.push('未发起 suggested-note 请求')

  await page.screenshot({
    path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\dash-note-modal.png',
  })
}

console.log()
console.log('控制台错误', errors.length, '条')
errors.slice(0, 5).forEach(e => console.log('  ·', e.slice(0, 140)))
console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  console.log('结论：PASS —— 首页建单弹窗已与客户管理页同机制预填理由')
}

await browser.close()
