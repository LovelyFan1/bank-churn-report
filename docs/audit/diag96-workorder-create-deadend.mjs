/**
 * 核查：工单管理页的「＋ 创建工单」是否是个死胡同。
 *
 * 疑点：该按钮调用 openCreate() 无参，弹窗里「客户编号」是 readonly 且为空。
 * 若无法填入客户，则 submitOrder 必然报「请填写客户信息」—— 该入口不可用。
 *
 * 只读：拦截所有写请求。
 */
import { chromium } from 'playwright-core'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } })
const page = await ctx.newPage()

const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })

const writes = []
await page.route('**/api/**', route => {
  const req = route.request()
  if (req.method() !== 'GET') {
    writes.push(`${req.method()} ${req.url().replace(/^.*\/api/, '/api')}`)
    return route.fulfill({ status: 201, contentType: 'application/json', body: '{}' })
  }
  return route.continue()
})

await page.goto('http://localhost:5173/work-orders', { waitUntil: 'networkidle' })
await page.waitForTimeout(1800)

const createBtn = page.locator('button', { hasText: '创建工单' }).first()
console.log('「创建工单」按钮存在:', await createBtn.count() > 0)
await createBtn.click()
await page.waitForTimeout(1200)

// 弹窗内的可编辑控件
const inputs = await page.locator('.modal input:not([readonly])').count()
const readonlyInputs = await page.locator('.modal input[readonly]').count()
const textareas = await page.locator('.modal textarea').count()
const selects = await page.locator('.modal select').count()

const cidVal = await page.locator('.modal input').first().inputValue()
const cidReadonly = await page.locator('.modal input').first().getAttribute('readonly')

console.log()
console.log('='.repeat(84))
console.log('弹窗内控件统计')
console.log('='.repeat(84))
console.log(`  客户编号值 = "${cidVal}"   readonly = ${cidReadonly !== null}`)
console.log(`  只读 input ${readonlyInputs} 个 · 可编辑 input ${inputs} 个`)
console.log(`  textarea ${textareas} 个 · select ${selects} 个`)

// 尝试点确认，看是否会报"请填写客户信息"
await page.locator('.modal-footer button.btn-primary').click()
await page.waitForTimeout(1200)

const toast = await page.locator('.toast').innerText().catch(() => '(无 toast)')
console.log()
console.log(`点「确认创建」后的提示：${toast}`)
console.log(`期间发出的写请求：${writes.length} 条`)

// 页面重新加载后，尝试从客户管理页跳过来是否带 customer 参数
console.log()
console.log('='.repeat(84))
const deadEnd = cidReadonly !== null && !cidVal
console.log(deadEnd
  ? '判定：**死胡同** —— 客户编号只读且为空，且无客户选择控件，无法建单。'
  : '判定：可填客户，不是死胡同。')
console.log(`控制台错误 ${errors.length} 条`)

await browser.close()
