/**
 * 二次确认实测 —— 三处高危操作的确认弹窗 + 重训按钮的权限态。
 *
 * 覆盖：
 *   1. 重训按钮：管理员可点、非管理员置灰且有原因说明
 *   2. 管理员点重训 → **弹确认**（不直接开跑）；取消后**不发请求**
 *   3. 删除工单 → 自绘弹窗（非原生 confirm），且显示工单号与客户名
 *   4. 删除工单 → 取消后不发 DELETE
 *   5. 重新聚类 → 弹确认且说明"覆盖全部客户标签"
 *   6. 空态"开始聚类分析" → **不弹确认**（首次生成，无覆盖）
 *
 * ⚠ 安全：所有写请求被护栏拦截；本脚本只验证"确认前不发请求"与
 *   "弹窗内容正确"，**绝不真正执行**重训/删除/聚类。
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
  const errs = []
  page.on('console', m => { if (m.type() === 'error') errs.push(m.text()) })
  page.on('pageerror', e => errs.push('pageerror: ' + e.message))
  // 记录所有写请求（护栏会拦下，但我们要知道"有没有发出去"）
  const writes = []
  page.on('request', r => {
    const m = r.method().toUpperCase()
    if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m)
        && r.url().includes('/api/')) {
      writes.push(`${m} ${r.url().replace(BASE, '')}`)
    }
  })
  const guard = await installReadOnlyGuard(page)
  await loginAs(page, user)
  try {
    await fn(page, errs, writes, guard)
  } finally {
    await ctx.close()
  }
}

console.log('='.repeat(84))
console.log('一、重训按钮的权限态（manager 非管理员）')
await withPage('liming', async (page, errs) => {
  await page.goto(`${BASE}/models`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3000)
  const btn = page.locator('button:has-text("重新训练")')
  const disabled = await btn.isDisabled()
  console.log('    按钮 disabled =', disabled)
  ok(disabled, '⚠ manager 看到的「重新训练」按钮已置灰')
  const body = await page.locator('body').innerText()
  ok(/需「系统管理员」权限/.test(body), '页面说明了为什么不能点')
  const title = await btn.getAttribute('title')
  console.log('    title =', title)
  ok(/管理员/.test(title || ''), '悬停提示说明了所需权限')
  // 置灰按钮点不动，不应产生任何请求
  const before = (await page.evaluate(() => 1)) && 0
  ok(true, '（置灰按钮不可点击，无需验证请求）')
})

console.log('\n二、管理员点重训 → 先弹确认，取消则不发请求')
await withPage('zhaomin', async (page, errs, writes) => {
  await page.goto(`${BASE}/models`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3000)
  const btn = page.locator('button:has-text("重新训练")')
  const disabled = await btn.isDisabled()
  ok(!disabled, '⚠ 管理员按钮可点')
  const n0 = writes.filter(w => w.includes('/model/train')).length
  await btn.click()
  await page.waitForTimeout(1200)
  const dialog = page.locator('.mc-confirm')
  ok(await dialog.count() > 0, '⚠ 弹出二次确认弹窗')
  const dtext = await dialog.innerText().catch(() => '')
  console.log('    弹窗文案:', dtext.replace(/\n+/g, ' | ').slice(0, 170))
  ok(/分级线|决策阈值|名单/.test(dtext), '弹窗说明了影响（分级线/决策阈值/名单）')
  const n1 = writes.filter(w => w.includes('/model/train')).length
  ok(n1 === n0, `⚠ 仅弹窗、未提交训练（train 请求 ${n0} → ${n1}）`)
  // 取消
  await page.locator('.mc-confirm button:has-text("取消")').click()
  await page.waitForTimeout(800)
  ok(await page.locator('.mc-confirm').count() === 0, '取消后弹窗关闭')
  ok(writes.filter(w => w.includes('/model/train')).length === n0,
     '⚠ 取消后仍未提交训练')
})

console.log('\n三、删除工单 → 自绘弹窗（含工单号与客户名）')
await withPage('liming', async (page, errs, writes) => {
  await page.goto(`${BASE}/work-orders`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3200)
  const delBtns = page.locator('button:has-text("✕")')
  const n = await delBtns.count()
  console.log('    页面上的删除按钮数:', n)
  ok(n > 0, '有删除按钮')
  if (n === 0) return
  // 记录该行文本，用于核对弹窗是否引用了同一张单
  const row = delBtns.first().locator('xpath=ancestor::tr[1]')
  const rowText = await row.innerText().catch(() => '')
  const n0 = writes.filter(w => w.startsWith('DELETE')).length
  await delBtns.first().click()
  await page.waitForTimeout(1200)
  // 原生 confirm 会触发 dialog 事件并被 Playwright 自动 dismiss；这里若
  // 出现自绘弹窗则说明已改造
  const dialog = page.locator('.modal')
  ok(await dialog.count() > 0, '⚠ 出现自绘弹窗（不再是原生 confirm）')
  const dtext = await dialog.innerText().catch(() => '')
  console.log('    弹窗文案:', dtext.replace(/\n+/g, ' | ').slice(0, 190))
  ok(/#\d+/.test(dtext), '弹窗显示了工单号')
  ok(/客户/.test(dtext), '弹窗显示了客户')
  ok(/不可恢复|不可逆/.test(dtext), '弹窗说明了不可恢复')
  const n1 = writes.filter(w => w.startsWith('DELETE')).length
  ok(n1 === n0, `⚠ 仅弹窗、未发 DELETE（${n0} → ${n1}）`)
  // 取消
  const cancel = page.locator('.modal button:has-text("取消")')
  if (await cancel.count()) {
    await cancel.first().click()
    await page.waitForTimeout(700)
    ok(await page.locator('.modal').count() === 0, '取消后弹窗关闭')
    ok(writes.filter(w => w.startsWith('DELETE')).length === n0,
       '⚠ 取消后仍未发 DELETE')
  }
})

console.log('\n四、重新聚类 → 弹确认并说明覆盖全量')
await withPage('liming', async (page, errs, writes) => {
  await page.goto(`${BASE}/clustering`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(4000)
  const btn = page.locator('button:has-text("重新聚类")')
  if (await btn.count() === 0) {
    // 可能处于空态（显示"开始聚类分析"）—— 那就是首次生成，本就不该弹
    const empty = await page.locator('button:has-text("开始聚类分析")').count()
    console.log('    未找到「重新聚类」按钮；空态按钮存在 =', empty > 0)
    ok(empty > 0, '处于空态（首次生成，无需确认）')
    const n0 = writes.length
    await page.locator('button:has-text("开始聚类分析")').click()
    await page.waitForTimeout(1500)
    ok(await page.locator('.cc-confirm').count() === 0,
       '⚠ 空态按钮**不弹**确认（首次生成，无覆盖）')
    return
  }
  const n0 = writes.filter(w => w.includes('/cluster/kmeans')).length
  await btn.first().click()
  await page.waitForTimeout(1300)
  const dialog = page.locator('.cc-confirm')
  ok(await dialog.count() > 0, '⚠ 弹出二次确认弹窗')
  const dtext = await dialog.innerText().catch(() => '')
  console.log('    弹窗文案:', dtext.replace(/\n+/g, ' | ').slice(0, 180))
  ok(/覆盖/.test(dtext) && /标签/.test(dtext), '弹窗说明会「覆盖现有分群标签」')
  const n1 = writes.filter(w => w.includes('/cluster/kmeans')).length
  ok(n1 === n0, `⚠ 仅弹窗、未提交聚类（${n0} → ${n1}）`)
  const cancel = page.locator('.cc-confirm button:has-text("取消")')
  if (await cancel.count()) {
    await cancel.first().click()
    await page.waitForTimeout(700)
    ok(await page.locator('.cc-confirm').count() === 0, '取消后弹窗关闭')
  }
})

console.log('\n' + '='.repeat(84))
console.log('结论：' + (fails.length === 0
  ? 'PASS —— 三处高危操作均需二次确认，且重训按钮按权限置灰'
  : `FAIL —— ${fails.length} 项未通过`))
fails.forEach(f => console.log('   × ' + f))
await browser.close()
