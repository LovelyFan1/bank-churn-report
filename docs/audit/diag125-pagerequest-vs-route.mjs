/**
 * 实测：page.request（APIRequestContext）会不会被 page.route 拦截？
 *
 * 为什么必须实测：我要给 diag118 造"一张 pending 工单"的夹具，
 *   计划用 page.request.post。若它**被 route 拦截**，夹具根本建不出来
 *   （只会拿到假响应）；若**不被拦截**，则会真实写库 ——
 *   两种情况对测试设计的影响完全相反，不能靠猜。
 *
 * 做法：注册护栏（默认拦所有写），然后用 page.request 发一个
 *   POST /api/work-orders，看护栏是否记录到它。
 *   无论结果如何，测后删除可能产生的真实工单。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext()
const page = await ctx.newPage()
const guard = await installReadOnlyGuard(page)

const resp = await page.request.post('http://localhost:8000/api/work-orders', {
  data: {
    customer_id: 'C062858', customer_name: 'Pirogov', geography: 'France',
    risk_level: 'CRITICAL', probability: 0.8883, balance: 225534.51,
    risk_factors: [], strategy: '客户经理上门 + 定制挽留方案',
    assignee: '', channel: 'relationship', value_tier_snapshot: 'HIGH',
    expected_value_snapshot: 200331.86,
  },
})

const status = resp.status()
const body = await resp.text()
let createdId = null
try { createdId = JSON.parse(body).id } catch (_) { /* 非 JSON */ }

console.log('=' .repeat(80))
console.log('page.request POST /api/work-orders')
console.log('  HTTP', status)
console.log('  body:', body.slice(0, 160))
console.log('  护栏是否记录到该请求:', guard.blocked.length > 0
  ? '是（被 route 拦截）' : '否（绕过了 route）')
console.log('  护栏记录:', guard.blocked)
console.log('=' .repeat(80))

if (status === 201 && createdId) {
  console.log('结论：page.request **绕过** page.route，真实写库')
  console.log('  → 造夹具可行，但必须测后删除；护栏不能保护它。')
  console.log('  → 产生的工单 id =', createdId, '（现在删除）')
  const del = await page.request.delete(
    `http://localhost:8000/api/work-orders/${createdId}`)
  console.log('  删除结果:', del.status())
} else if (status === 409) {
  console.log('结论：page.request 绕过 route（拿到真实 409），该客户已有进行中工单')
} else {
  console.log('结论：page.request 被拦或异常，需人工判断')
}

await browser.close()
