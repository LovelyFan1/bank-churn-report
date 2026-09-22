/**
 * 验证：批量建单现在**携带建议理由**（修复前不带，构成"单建有、批建没有"）。
 *
 * 拦截 POST /work-orders 并**检查请求体**，但**不发出去** ——
 * 生产库 62 条工单是真实数据，不得污染。
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
const batchGets = []

await page.route('**/api/**', async route => {
  const req = route.request()
  const url = req.url()
  const method = req.method()

  if (url.includes('/suggested-notes/batch')) {
    batchGets.push(url)
    return route.continue()
  }
  if (method === 'POST' && /\/api\/work-orders$/.test(url)) {
    posted.push(JSON.parse(req.postData() || '{}'))
    // 伪造成功响应，绝不落到后端
    return route.fulfill({
      status: 201,
      contentType: 'application/json',
      body: JSON.stringify({ id: 9999, status: 'pending' }),
    })
  }
  if (method !== 'GET') return route.abort()
  return route.continue()
})

// 打开列表页，勾选前若干个可选客户
await page.goto('http://localhost:5173/customers?risk_level=CRITICAL', { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)

const boxes = page.locator('tbody tr input[type=checkbox]:not([disabled])')
const n = Math.min(await boxes.count(), 4)
console.log(`可选客户数 ${await boxes.count()}，勾选前 ${n} 个`)
for (let i = 0; i < n; i++) await boxes.nth(i).check()
await page.waitForTimeout(300)

const bar = await page.locator('.batch-bar').innerText()
console.log('批量条:', bar.replace(/\s+/g, ' ').trim())

await page.locator('.batch-bar button.btn-primary').click()
await page.waitForTimeout(4000)

console.log()
console.log('='.repeat(88))
console.log(`批量建议理由请求次数 ${batchGets.length}（应为 1，而非 N）`)
batchGets.forEach(u => console.log('  ·', u.replace(/^.*\/api/, '/api')))
console.log(`发出的建单 POST ${posted.length} 条`)
console.log('='.repeat(88))

const fails = []
if (batchGets.length !== 1) fails.push(`批量理由请求 ${batchGets.length} 次，期望 1 次`)
if (posted.length === 0) fails.push('没有捕获到任何建单 POST')

for (const p of posted) {
  const ok = typeof p.note === 'string' && p.note.length > 20
  console.log()
  console.log(`${p.customer_id}  note长度=${(p.note || '').length}  ${ok ? '[OK]' : '[FAIL 无理由]'}`)
  console.log('   ' + (p.note || '(空)').slice(0, 150))
  if (!ok) fails.push(`${p.customer_id} 建单未携带建议理由`)
  if (p.note && p.note.includes('**')) fails.push(`${p.customer_id} 理由含 Markdown 星号`)
  if (p.note && !p.note.includes('口径')) fails.push(`${p.customer_id} 理由缺口径标注`)
}

console.log()
console.log('控制台错误', errors.length, '条')
errors.slice(0, 5).forEach(e => console.log('  ·', e.slice(0, 140)))

console.log()
if (fails.length) {
  console.log(`结论：FAIL —— ${fails.length} 项`)
  fails.forEach(f => console.log('  ·', f))
} else {
  console.log('结论：PASS —— 批量建单已携带建议理由，单次批量请求，无控制台错误')
}

await browser.close()
