/**
 * 匿名化 UI 端到端 —— 验证只读角色看得到统计、看不到身份，且页面不崩。
 *
 * 覆盖：
 *   1. viewer 登录 → 客户名单：有匿名提示、无姓名/编号/余额、无导出/建单按钮
 *   2. viewer 工作台：Top10 匿名、聚合数字仍在、**无白屏/控制台报错**
 *   3. viewer 工单页：客户列匿名、状态只读、无编辑/删除按钮
 *   4. manager 登录 → 三页均正常显示姓名（回归）
 *
 * ⚠ 只读：安装共享护栏，所有写请求被拦。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, loginAs } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'
const BASE = 'http://localhost:5173'

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const fails = []
const ok = (c, m) => { console.log(`  [${c ? 'OK ' : 'FAIL'}] ${m}`); if (!c) fails.push(m) }

async function withPage(user, fn) {
  const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
  const page = await ctx.newPage()
  const errors = []
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
  page.on('pageerror', e => errors.push('pageerror: ' + e.message))
  const guard = await installReadOnlyGuard(page)
  await loginAs(page, user)
  try {
    await fn(page, errors, guard)
  } finally {
    await ctx.close()
  }
}

const hasName = (text) =>
  /Bentley|Goddard|Pirogov|Taylor|Coates|Siciliani|Clark/.test(text)

console.log('='.repeat(84))
console.log('一、viewer（chenjie）—— 客户名单')
await withPage('chenjie', async (page, errors) => {
  await page.goto(`${BASE}/customers`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3000)
  const body = await page.locator('body').innerText()
  ok(await page.locator('.mask-banner').count() > 0, '有脱敏提示条')
  ok(!hasName(body), '⚠ 页面无任何客户姓名')
  ok(!/C\d{6}/.test(body), '⚠ 页面无任何客户编号')
  ok(/客户 #\d+/.test(body), '显示匿名序号（客户 #N）')
  ok(/极高|高危|中等|低风险/.test(body), '风险等级仍可见')
  ok(/高价值|低价值|零余额/.test(body), '价值层仍可见')
  // 按钮应消失
  const exportBtn = await page.locator('button:has-text("导出")').count()
  ok(exportBtn === 0, '⚠ 无「导出」按钮（后端也会 403）')
  const createBtn = await page.locator('button:has-text("创建工单")').count()
  ok(createBtn === 0, '⚠ 无「创建工单」按钮')
  ok(errors.length === 0, `无控制台错误（实际 ${errors.length}）`)
  if (errors.length) errors.slice(0, 3).forEach(e => console.log('   !', e.slice(0, 130)))
})

console.log('\n二、viewer —— 工作台（重点：脱敏后不得白屏）')
await withPage('chenjie', async (page, errors) => {
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3500)
  const body = await page.locator('body').innerText()
  // 白屏判据：页面上应仍有内容
  ok(body.length > 400, `页面有实质内容（${body.length} 字符，非白屏）`)
  ok(!hasName(body), '⚠ Top10 无客户姓名')
  // 聚合数字仍在
  ok(/96,?418/.test(body), '客户总数仍可见')
  ok(/极高|高危|中等|低风险/.test(body), '风险分布仍可见')
  ok(errors.length === 0, `无控制台错误（实际 ${errors.length}）`)
  if (errors.length) errors.slice(0, 4).forEach(e => console.log('   !', e.slice(0, 160)))
})

console.log('\n三、viewer —— 工单页')
await withPage('chenjie', async (page, errors) => {
  await page.goto(`${BASE}/work-orders`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3000)
  const body = await page.locator('body').innerText()
  ok(body.length > 300, `页面有内容（${body.length} 字符）`)
  ok(!hasName(body), '⚠ 无客户姓名')
  ok(!/C\d{6}/.test(body), '⚠ 无客户编号')
  const delBtn = await page.locator('button:has-text("✕")').count()
  const editBtn = await page.locator('button:has-text("✎")').count()
  ok(delBtn === 0 && editBtn === 0, '⚠ 无编辑/删除按钮')
  const sel = await page.locator('.status-select').count()
  ok(sel === 0, '⚠ 状态下拉被替换为只读文字')
  ok(errors.length === 0, `无控制台错误（实际 ${errors.length}）`)
  if (errors.length) errors.slice(0, 3).forEach(e => console.log('   !', e.slice(0, 130)))
})

console.log('\n四、manager（liming）—— 回归：三页均正常显示姓名')
await withPage('liming', async (page, errors) => {
  await page.goto(`${BASE}/customers`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3000)
  let body = await page.locator('body').innerText()
  ok(hasName(body), '客户名单显示姓名')
  ok(/C\d{6}/.test(body), '客户名单显示编号')
  ok(await page.locator('.mask-banner').count() === 0, '无脱敏提示（manager 不需要）')
  ok(await page.locator('button:has-text("导出")').count() > 0, '导出按钮可见')

  await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3000)
  body = await page.locator('body').innerText()
  ok(hasName(body), '工作台 Top10 显示姓名')

  await page.goto(`${BASE}/work-orders`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3000)
  body = await page.locator('body').innerText()
  ok(hasName(body), '工单页显示客户名')
  ok(await page.locator('.status-select').count() > 0, '状态下拉可用')
  ok(errors.length === 0, `无控制台错误（实际 ${errors.length}）`)
  if (errors.length) errors.slice(0, 3).forEach(e => console.log('   !', e.slice(0, 130)))
})

console.log('\n' + '='.repeat(84))
console.log('结论：' + (fails.length === 0
  ? 'PASS —— 只读角色全站匿名且页面正常，客户经理不受影响'
  : `FAIL —— ${fails.length} 项未通过`))
fails.forEach(f => console.log('   × ' + f))

await browser.close()
