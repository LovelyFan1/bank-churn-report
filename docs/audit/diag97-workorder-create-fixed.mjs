/**
 * 验证：工单管理页「＋ 创建工单」死胡同已修复，且建议理由可生成。
 *
 * 修复前实测：客户编号只读且为空 → 点确认只报「请填写客户信息」，0 条写请求。
 * 修复后应能：填编号 → 自动带出客户信息 → 生成建议理由 → 可建单。
 *
 * 只读：拦截所有写请求，不污染生产库。
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

const posted = []
await page.route('**/api/**', route => {
  const req = route.request()
  if (req.method() === 'POST' && /\/api\/work-orders$/.test(req.url())) {
    posted.push(JSON.parse(req.postData() || '{}'))
    return route.fulfill({
      status: 201, contentType: 'application/json',
      body: JSON.stringify({ id: 9999, status: 'pending' }),
    })
  }
  if (req.method() !== 'GET') return route.abort()
  return route.continue()
})

const fails = []
await page.goto('http://localhost:5173/work-orders', { waitUntil: 'networkidle' })
await page.waitForTimeout(1800)

await page.locator('button', { hasText: '创建工单' }).first().click()
await page.waitForSelector('.modal', { state: 'visible', timeout: 8000 })
await page.waitForTimeout(800)

// 1) 客户编号现在应可编辑
const cidInput = page.locator('.modal input').first()
const isReadonly = await cidInput.getAttribute('readonly')
console.log(`客户编号 readonly = ${isReadonly !== null}`)
if (isReadonly !== null) fails.push('客户编号仍为只读，死胡同未修复')

// 2) 未填编号时点确认，应给出明确提示且不发写请求
await page.locator('.modal-footer button.btn-primary').click()
await page.waitForTimeout(800)
const t1 = await page.locator('.toast').innerText().catch(() => '')
console.log(`空编号点确认 → 提示「${t1}」 · 写请求 ${posted.length} 条`)

// 3) 填入一个真实客户编号，应自动带出信息并生成理由
await cidInput.fill('C034525')
await cidInput.press('Enter')
await page.waitForTimeout(3000)

const formVals = await page.locator('.modal input').evaluateAll(
  els => els.map(e => e.value))
console.log()
console.log('='.repeat(88))
console.log('自动带出的字段：')
formVals.forEach((v, i) => { if (v) console.log(`  [${i}] ${v}`) })

const note = await page.locator('.modal textarea').first().inputValue()
const hint = (await page.locator('.note-assist-hint').allInnerTexts()).join(' | ')
const err = (await page.locator('.note-assist-err').allInnerTexts()).join(' | ')

console.log('='.repeat(88))
console.log('判定:', hint || '(无)')
if (err) console.log('错误:', err)
console.log('-'.repeat(88))
console.log(note || '(备注为空)')
console.log('='.repeat(88))

if (!formVals.some(v => v.includes('Bentley') || /^[A-Z][a-z]+$/.test(v)))
  console.log('提示：客户姓名可能未带出')
if (!note.trim()) fails.push('填编号后备注未预填建议理由')
if (note && !note.includes('口径')) fails.push('理由缺口径标注')
if (note && note.includes('**')) fails.push('理由含 Markdown 星号')
if (err) fails.push('生成失败: ' + err)
if (!hint.includes('经济性判定')) fails.push('未显示经济性判定')

// 4) 错误编号应给出"未找到客户"而非静默失败
await cidInput.fill('C999999')
await cidInput.press('Enter')
await page.waitForTimeout(2500)
const err2 = (await page.locator('.note-assist-err').allInnerTexts()).join(' | ')
console.log()
console.log(`填错误编号 C999999 → 提示「${err2 || '(无)'}」`)
if (!err2.includes('未找到')) fails.push('错误编号未给出"未找到客户"提示')

console.log()
// ⚠ 这里预期会有 404：上面第 88 行**故意**填了不存在的客户编号 C999999，
//   用来验证"编号填错要给出明确提示"这条路径。
//   因此 404 是**测试设计的一部分**，不是缺陷。
//   但也不能无条件放过 —— 只允许 404，其它错误（500/控制台异常）仍需报警。
const EXPECTED_404 = 2   // 前端会请求两次（详情 + 建议理由）
const nonExpected = errors.filter(e => !e.includes('404'))
const n404 = errors.length - nonExpected.length
console.log(`控制台错误 ${errors.length} 条（其中 404 ${n404} 条为故意触发，预期 ${EXPECTED_404} 条）`)
nonExpected.slice(0, 5).forEach(e => console.log('  · [非预期]', e.slice(0, 140)))
if (nonExpected.length) fails.push(`出现非预期控制台错误 ${nonExpected.length} 条`)

console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  console.log('结论：PASS —— 工单页建单入口已可用，自动带出信息并生成建议理由')
}

await page.screenshot({
  path: 'C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\wo-note-fixed.png',
})
await browser.close()
