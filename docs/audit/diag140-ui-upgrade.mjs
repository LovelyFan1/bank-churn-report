/**
 * 验证前端改版：表头吸顶 + 面包屑 + 数字等宽。
 *
 * 核心断言（都是"实测行为"而非"样式声明"）：
 *   1. 表头 sticky 在**滚动后仍然可见**（这是本轮最容易做错的一点 ——
 *      祖先 overflow:hidden 会让 sticky 静默失效）
 *   2. 面包屑：一级页**不出现**，二级页出现且能点回父级
 *   3. 数字列 font-variant-numeric = tabular-nums
 *   4. 无业务回归：表格行数、统计数字、分页都正常；零控制台错误
 *
 * ⚠ 只读：安装共享护栏。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, loginAs } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'
const BASE = 'http://localhost:5173'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 800 } })
const page = await ctx.newPage()
const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', e => errors.push('pageerror: ' + e.message))
await installReadOnlyGuard(page)
await loginAs(page, 'liming')

const fails = []
const ok = (c, m) => { console.log(`  [${c ? 'OK ' : 'FAIL'}] ${m}`); if (!c) fails.push(m) }

console.log('='.repeat(80))
console.log('一、客户名单：表头吸顶（页面滚动后仍钉在顶栏下沿）')
await page.goto(`${BASE}/customers`, { waitUntil: 'networkidle' })
await page.waitForTimeout(3200)

const before = await page.evaluate(() => {
  const th = document.querySelector('thead th')
  return { top: Math.round(th.getBoundingClientRect().top), rows: document.querySelectorAll('tbody tr').length }
})
console.log(`    滚动前：表头 top=${before.top}，行数=${before.rows}`)
ok(before.rows > 0, `表格有数据（${before.rows} 行）`)

/*
 * ⚠ 断言已按**最终实现**更新（不是放宽）。
 *
 * 我第一版给表格容器加了 max-height，断言"容器内能滚动"。实测发现那会
 * 造成**双滚动条**（页面一条 + 容器一条）—— 用户滚轮在表格里滚表格、
 * 在外面滚页面，是 B 端反模式。
 *
 * 最终实现改为**页面级滚动 + 表头 sticky 到视口（顶栏下沿）**，
 * 故此处断言也改为验证这个行为：
 *   · 页面可滚动
 *   · 表格容器**不**产生内滚动（否则又是双滚动条）
 *   · 滚动后表头正好停在顶栏下沿
 */
const after = await page.evaluate(async () => {
  const box = document.querySelector('.table-scroll')
  if (!box) return { noBox: true }
  window.scrollTo(0, 600)
  await new Promise(r => setTimeout(r, 450))
  const th = document.querySelector('thead th')
  const tb = document.querySelector('.topbar').getBoundingClientRect()
  const r = th.getBoundingClientRect()
  return {
    pageScrollable: document.documentElement.scrollHeight > window.innerHeight,
    innerScrollable: box.scrollHeight > box.clientHeight + 1,
    thTop: Math.round(r.top),
    topbarBottom: Math.round(tb.bottom),
    thVisible: r.top >= 0 && r.bottom > 0 && r.height > 0,
  }
})
console.log('    滚动后:', JSON.stringify(after))
ok(!after.noBox, '存在 .table-scroll 容器')
ok(after.pageScrollable, '页面可滚动')
ok(!after.innerScrollable, '⚠ 表格容器无内滚动（避免双滚动条）')
ok(after.thVisible, '⚠ 表头仍可见（sticky 生效）')
ok(Math.abs(after.thTop - after.topbarBottom) < 3,
   `⚠ 表头正好停在顶栏下沿（表头 top=${after.thTop}，顶栏 bottom=${after.topbarBottom}）`)

console.log('\n二、面包屑：一级页不显示')
const bc1 = await page.locator('.breadcrumb').count()
console.log('    客户名单页面包屑数量:', bc1)
ok(bc1 === 0, '⚠ 一级页不显示面包屑（避免与侧边栏冗余）')

console.log('\n三、面包屑：二级页显示并能返回')
await page.goto(`${BASE}/customers/C071081`, { waitUntil: 'networkidle' })
await page.waitForTimeout(3000)
const bc2 = await page.locator('.breadcrumb').count()
ok(bc2 > 0, '⚠ 客户详情页出现面包屑')
if (bc2 > 0) {
  const t = await page.locator('.breadcrumb').innerText()
  console.log('    面包屑内容:', t.replace(/\n/g, ' '))
  ok(/客户名单/.test(t), '含父级「客户名单」')
  ok(/客户详情/.test(t), '含当前页「客户详情」')
  const link = page.locator('.breadcrumb .bc-link')
  ok(await link.count() > 0, '父级为可点链接')
  await link.first().click()
  await page.waitForTimeout(2200)
  console.log('    点击后 URL:', page.url())
  ok(page.url().endsWith('/customers'), '⚠ 点父级能回到客户名单')
}

console.log('\n四、数字等宽（tabular-nums）')
await page.goto(`${BASE}/customers`, { waitUntil: 'networkidle' })
await page.waitForTimeout(3000)
const fv = await page.evaluate(() => {
  const td = document.querySelector('tbody td')
  const th = document.querySelector('thead th')
  return {
    td: td ? getComputedStyle(td).fontVariantNumeric : null,
    th: th ? getComputedStyle(th).fontVariantNumeric : null,
  }
})
console.log('    tbody td =', fv.td, '| thead th =', fv.th)
ok(fv.td === 'tabular-nums', '⚠ 表格单元格启用等宽数字')

console.log('\n五、挽留工单：吸顶 + 展开详情不被裁剪')
await page.goto(`${BASE}/work-orders`, { waitUntil: 'networkidle' })
await page.waitForTimeout(3200)
const wo = await page.evaluate(async () => {
  const box = document.querySelector('.table-scroll')
  const rows = document.querySelectorAll('tbody tr').length
  window.scrollTo(0, 400)
  await new Promise(r => setTimeout(r, 400))
  const th = document.querySelector('thead th')
  const r = th.getBoundingClientRect()
  return {
    rows,
    innerScrollable: box.scrollHeight > box.clientHeight + 1,
    thVisible: r.top >= 0 && r.bottom > 0,
  }
})
console.log('    ', JSON.stringify(wo))
ok(wo.rows > 0, `工单表格有数据（${wo.rows} 行）`)
ok(!wo.innerScrollable, '⚠ 工单表格容器无内滚动（与客户名单一致）')
ok(wo.thVisible, '⚠ 工单表头吸顶生效')

// 展开第一行详情，确认面板可见（不被 max-height 裁掉）
const firstRow = page.locator('tbody tr').first()
if (await firstRow.count()) {
  await firstRow.click()
  await page.waitForTimeout(900)
  const panel = page.locator('.detail-panel')
  const cnt = await panel.count()
  ok(cnt > 0, '展开后出现详情面板')
  if (cnt > 0) {
    const vis = await panel.first().isVisible()
    ok(vis, '⚠ 详情面板可见（未被滚动容器裁掉）')
  }
}

console.log('\n六、业务无回归：统计数字与分页正常')
await page.goto(`${BASE}/customers`, { waitUntil: 'networkidle' })
await page.waitForTimeout(3000)
const stat = await page.evaluate(() => {
  const cards = [...document.querySelectorAll('.glass-card')]
  const total = document.body.innerText.match(/96,418/)
  const pager = document.body.innerText.match(/第\s*\d+\s*\/\s*[\d,]+\s*页/)
  return { hasTotal: !!total, hasPager: !!pager }
})
ok(stat.hasTotal, '总客户数 96,418 正常显示')
ok(stat.hasPager, '分页信息正常显示')

console.log('\n' + '='.repeat(80))
console.log('控制台错误:', errors.length)
errors.slice(0, 5).forEach(e => console.log('   !', e.slice(0, 150)))
ok(errors.length === 0, `无控制台错误（实际 ${errors.length}）`)

console.log('\n结论：' + (fails.length === 0
  ? 'PASS —— 吸顶/面包屑/等宽数字均生效，业务无回归'
  : `FAIL —— ${fails.length} 项未通过`))
fails.forEach(f => console.log('   × ' + f))
await browser.close()
