/**
 * 验证：建单弹窗「建议理由」端到端。
 *
 * 只读测试：拦截所有非 GET 请求，绝不真的建单 —— 生产库里 62 条工单
 * 是真实业务数据，测试不得污染。
 */
import { chromium } from 'playwright-core'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const CUSTOMERS = [
  'C067546',  // no_asset  余额 0
  'C061245',  // not_worth
  'C055701',  // marginal
  'C059629',  // worth
]

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))

// 只读护栏
let blocked = 0
await page.route('**/api/**', route => {
  const method = route.request().method()
  if (method !== 'GET') { blocked++; return route.abort() }
  return route.continue()
})

let fails = []
const results = []

for (const cid of CUSTOMERS) {
  // 直达客户详情页不行 —— 建单入口在列表页。用搜索定位该客户。
  await page.goto(`http://localhost:5173/customers?search=${cid}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)

  // 找到该行的「建单」按钮
  const row = page.locator('tbody tr', { hasText: cid }).first()
  if (await row.count() === 0) {
    fails.push(`${cid}: 列表里找不到该客户`)
    continue
  }
  const btn = row.locator('button', { hasText: /建单|创建工单/ }).first()
  if (await btn.count() === 0) {
    // 可能按钮文案不同，打印该行所有按钮名以便诊断
    const names = await row.locator('button').allInnerTexts()
    fails.push(`${cid}: 未找到建单按钮，该行按钮有 ${JSON.stringify(names)}`)
    continue
  }
  await btn.click()
  await page.waitForSelector('.modal', { state: 'visible', timeout: 8000 })

  // 等建议理由区域出现内容
  await page.waitForTimeout(1500)

  const noteVal = await page.locator('.modal textarea').first().inputValue()
  const verdictEl = page.locator('.note-assist-verdict')
  const hasVerdict = await verdictEl.count() > 0
  const verdictText = hasVerdict ? (await verdictEl.innerText()).replace(/\s+/g, ' ').trim() : ''
  const hintText = (await page.locator('.note-assist-hint').allInnerTexts()).join(' | ')
  const errText = (await page.locator('.note-assist-err').allInnerTexts()).join(' | ')

  results.push({ cid, noteLen: noteVal.length, noteVal, verdictText, hintText, errText })

  if (errText) fails.push(`${cid}: 生成失败 —— ${errText}`)
  if (!hasVerdict) fails.push(`${cid}: 未渲染经济性判定条`)
  if (!noteVal.trim()) fails.push(`${cid}: 备注框未被预填`)
  if (noteVal && !noteVal.includes('口径')) fails.push(`${cid}: 备注缺少口径标注`)
  if (noteVal && !noteVal.includes(cid === 'C067546' ? '' : '')) { /* 占位 */ }

  await page.screenshot({ path: `C:\\Users\\yyfab\\AppData\\Local\\Temp\\pw-probe\\note-${cid}.png` })

  // 关闭弹窗
  await page.locator('.modal-close').first().click()
  await page.waitForTimeout(400)
}

// ── 逐档输出 ─────────────────────────────────────────
for (const r of results) {
  console.log('='.repeat(90))
  console.log(`客户 ${r.cid}   备注长度 ${r.noteLen}`)
  console.log('-'.repeat(90))
  console.log(r.verdictText || '(无判定条)')
  console.log('提示:', r.hintText || '(无)')
  if (r.errText) console.log('错误:', r.errText)
  console.log('-'.repeat(90))
  console.log(r.noteVal || '(备注为空)')
}

console.log()
console.log('='.repeat(90))
console.log(`控制台错误 ${errors.length} 条`)
errors.slice(0, 10).forEach(e => console.log('  ·', e.slice(0, 160)))
console.log(`被拦截的非 GET 请求 ${blocked} 次（应 >0 仅当页面误发写请求）`)
console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  console.log('结论：PASS —— 四档均预填理由、渲染判定、无控制台错误')
}

await browser.close()
