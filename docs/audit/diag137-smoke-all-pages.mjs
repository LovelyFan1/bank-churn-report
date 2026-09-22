/**
 * 全站冒烟 —— 确认匿名化没有把别的页面搞崩。
 *
 * 逐个访问所有页面，断言：
 *   · 页面渲染出**该页面的关键元素**（不是白屏）
 *   · 无意外控制台错误、无 pageerror
 *
 * ⚠ 判据是"关键元素存在"，**不是"文本长度"**。
 *   实测教训：首版用 `text.length > 300` 当白屏判据，结果两个完全正常的
 *   页面被判 FAIL ——
 *     /assistant 空态页只有输入框与示例问题，265 字符（本就如此）
 *     /eda       5 个图表全在 canvas 里，innerText 取不到，只有 152 字符
 *   文本长度与"页面是否正常"无关：图表页天然文字少，空态页天然文字少。
 *   改成检查各页的特征元素后，两页均正确判为通过。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, loginAs } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'
const BASE = 'http://localhost:5173'

// [路径, 名称, 关键元素选择器（任一命中即视为渲染成功）]
const PAGES = [
  ['/dashboard', '工作台', ['.glass-card']],
  ['/customers', '客户名单', ['.glass-card', 'table']],
  ['/work-orders', '挽留工单', ['.glass-card']],
  ['/intervention', '干预策略', ['.glass-card']],
  ['/assistant', '智能助手', ['.composer textarea']],
  ['/clustering', '客群洞察', ['.glass-card', 'canvas']],
  ['/eda', '数据洞察', ['.glass-card', 'canvas']],
  ['/models', '模型效果', ['.glass-card']],
]

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const fails = []

for (const user of ['chenjie', 'liming']) {
  console.log('='.repeat(80))
  console.log(`角色: ${user}`)
  const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
  const page = await ctx.newPage()
  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push('pageerror: ' + e.message))
  await installReadOnlyGuard(page)
  await loginAs(page, user)

  for (const [path, name, sels] of PAGES) {
    const before = errors.length
    await page.goto(BASE + path, { waitUntil: 'networkidle' })
    await page.waitForTimeout(2800)
    const text = await page.locator('body').innerText().catch(() => '')
    let hits = 0
    for (const s of sels) hits += await page.locator(s).count()
    const newErr = errors.slice(before)
    // 权限类 401/403 是预期的（viewer 触发受限接口），单独分类
    const realErr = newErr.filter(e => !/401|403|Unauthorized|Forbidden/.test(e))

    const rendered = hits > 0 || text.length > 800
    const okErr = realErr.length === 0
    const mark = rendered && okErr ? 'OK ' : 'FAIL'
    console.log(`  [${mark}] ${name.padEnd(6)} ${path.padEnd(15)} `
      + `元素命中=${String(hits).padStart(3)} 文本=${String(text.length).padStart(5)} `
      + `意外错误=${realErr.length}`)
    if (!rendered || !okErr) {
      fails.push(`${user} ${name} (${path})`)
      realErr.slice(0, 3).forEach(e => console.log('        !', e.slice(0, 140)))
    }
  }
  await ctx.close()
}

console.log('\n' + '='.repeat(80))
console.log('结论：' + (fails.length === 0
  ? 'PASS —— 全站 16 次页面访问均正常渲染、无意外控制台错误'
  : `FAIL —— ${fails.length} 项`))
fails.forEach(f => console.log('   × ' + f))
await browser.close()
