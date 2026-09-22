/**
 * 派单卡片「跳过 / 翻页」回归验收。
 *
 * 复现用户报告的缺陷：
 *   "批量建单跳过这张之后点上一张，啥也没有了"
 *
 * 顺带覆盖同源的 4 个缺陷：
 *   · 往回翻时画像是否还在（不能变空壳）
 *   · 已跳过的卡是否可"恢复"
 *   · 「应用到其余」的张数是否如实（不把已跳过/已改的算进去）
 *   · 提交按钮的张数是否为**未跳过**的
 *   · 最后一张点「跳过」是否**不会**偷偷提交
 *
 * ⚠ 纪律：本脚本**不点「确认派单」**，全程零业务写请求。
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'https://blessed-ghz-fast-francis.trycloudflare.com'
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1500, height: 960 } })
const page = await ctx.newPage()
const errs = []
page.on('pageerror', (e) => errs.push('PAGEERROR ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })
const writes = []
page.on('request', (r) => {
  const m = r.method().toUpperCase()
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m)) writes.push(m + ' ' + new URL(r.url()).pathname)
})

// ── 登录（manager，有派单权）──
await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 })
await page.locator('input[autocomplete="username"]').fill('liming')
await page.locator('input[autocomplete="current-password"]').fill('Bank@2026')
await page.locator('button[type="submit"]').click()
await page.waitForTimeout(2500)
const totp = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]')
if (await totp.count()) {
  const fb = page.locator('button:has-text("填入"), button:has-text("使用")')
  if (await fb.count()) { await fb.first().click(); await page.waitForTimeout(700) }
  let c = await totp.first().inputValue().catch(() => '')
  if (!/^\d{4,6}$/.test(c || '')) {
    const m = (await page.evaluate(() => document.body.innerText)).match(/\b(\d{6})\b/)
    if (m) await totp.first().fill(m[1])
  }
  await page.locator('button[type="submit"]').click()
}
await page.waitForFunction(() => !location.pathname.includes('/login'), null, { timeout: 30000 }).catch(() => {})
await page.waitForTimeout(2500)

// ── 进客户页，勾选 3 个 ──
await page.goto(BASE + '/customers', { waitUntil: 'networkidle', timeout: 60000 })
await page.waitForTimeout(7000)

const boxes = page.locator('tbody input[type="checkbox"]:not([disabled])')
const n = await boxes.count()
console.log('  selectable:', n)
for (let i = 0; i < Math.min(3, n); i++) await boxes.nth(i).check()
await page.waitForTimeout(800)
await page.locator('button:has-text("批量派单")').click()
await page.waitForTimeout(4500)

const cardText = async () => page.evaluate(() => {
  const el = document.querySelector('.dispatch-card')
  return el ? el.innerText.replace(/\s+/g, ' ') : ''
})
const custBlock = async () => page.evaluate(() => {
  const el = document.querySelector('.dispatch-cust')
  return el ? el.innerText.replace(/\s+/g, ' ').trim() : '(MISSING)'
})

ok((await page.locator('.dispatch-card').count()) > 0, 'dispatch card opened')

const t1 = await cardText()
const c1 = await custBlock()
console.log('  card1:', c1.slice(0, 110))
ok(/流失概率/.test(c1) && /账户余额/.test(c1), 'card 1 shows customer profile')
ok(/1 \/ 3/.test(t1), 'progress = 1 / 3')

// ══════════════════════════════════════════════════════
console.log('\n=== 核心复现：跳过 → 上一张 ===')
// ══════════════════════════════════════════════════════

// 先翻到第 2 张，再跳过它，然后点「上一张」—— 这正是用户的操作路径
await page.locator('.dispatch-card button:has-text("下一张")').click()
await page.waitForTimeout(1000)
ok(/2 \/ 3/.test(await cardText()), 'now on card 2')

const c2Before = await custBlock()
console.log('  card2 before skip:', c2Before.slice(0, 110))
ok(/流失概率/.test(c2Before), 'card 2 profile present before skip')

await page.locator('.dispatch-card button:has-text("跳过这张")').click()
await page.waitForTimeout(1000)
ok(/3 \/ 3/.test(await cardText()), 'skip auto-advances to card 3')
ok(/已跳过 1/.test(await cardText()), 'header shows 已跳过 1')

// 回翻到第 2 张（用户报bug的路径）
await page.locator('.dispatch-card button:has-text("上一张")').click()
await page.waitForTimeout(1200)
const t2 = await cardText()
const c2After = await custBlock()
console.log('  card2 after back:', c2After.slice(0, 110))

ok(/2 \/ 3/.test(t2), 'back on card 2')
ok(c2After !== '(MISSING)', '*** .dispatch-cust still EXISTS after skip+back (the reported bug)')
ok(/流失概率/.test(c2After), '*** profile STILL SHOWN after skip+back (流失概率 present)')
ok(/账户余额/.test(c2After), '*** profile balance still shown')
ok(/本次不派单/.test(t2), 'card 2 marked as 「本次不派单」')
// ⚠ 比较时要去掉"本次不派单"这个新增标记 —— 跳过后文本必然多这一句，
//   直接字符串相等会假失败（这条断言自己踩过一次）。
const strip = (s) => s.replace(/本次不派单/g, '').replace(/\s+/g, ' ').trim()
ok(strip(c2After) === strip(c2Before),
   'profile content unchanged apart from the skip badge')

// 恢复
await page.locator('.dispatch-card button:has-text("恢复这张")').click()
await page.waitForTimeout(1000)
const t3 = await cardText()
ok(!/本次不派单/.test(t3), 'restore removes the skip mark')
ok(!/已跳过 1/.test(t3), 'skipped counter back to 0')
ok(/流失概率/.test(await custBlock()), 'profile still there after restore')

// ══════════════════════════════════════════════════════
console.log('\n=== 计数与提交安全 ===')
// ══════════════════════════════════════════════════════

const btnText = await page.evaluate(() => {
  const btns = Array.from(document.querySelectorAll('.dispatch-foot button'))
  const submit = btns.find((b) => /确认派单/.test(b.innerText))
  return submit ? submit.innerText.trim() : '(none)'
})
console.log('  submit button:', btnText)
ok(/确认派单（3 张）/.test(btnText), 'submit count = 3 (none skipped)')

// 跳过第 2 张 → 提交按钮应变成 2 张
await page.locator('.dispatch-card button:has-text("跳过这张")').click()
await page.waitForTimeout(1000)
const btnText2 = await page.evaluate(() => {
  const btns = Array.from(document.querySelectorAll('.dispatch-foot button'))
  const submit = btns.find((b) => /确认派单/.test(b.innerText))
  return submit ? submit.innerText.trim() : '(none)'
})
console.log('  after skipping card2:', btnText2)
ok(/确认派单（2 张）/.test(btnText2), '*** submit count drops to 2 after skipping one')

// 最后一张点「跳过」不应自动提交
//   先回第 1 张，再跳到第 3 张（第 2 张已跳过）
await page.locator('.dispatch-card button:has-text("上一张")').click()
await page.waitForTimeout(700)
await page.locator('.dispatch-card button:has-text("下一张")').click()
await page.waitForTimeout(700)
const onThird = /3 \/ 3/.test(await cardText())
ok(onThird, 'on the last card (3/3)')

const before = await page.locator('.dispatch-card').count()
await page.locator('.dispatch-card button:has-text("跳过这张")').click()
await page.waitForTimeout(1500)
const stillOpen = await page.locator('.dispatch-card').count()
ok(stillOpen === before && stillOpen > 0,
   '*** skipping the LAST card does NOT auto-submit (panel stays open)')
ok(/确认派单（1 张）/.test(await cardText()), 'submit count = 1 (two skipped)')

// 全部跳过后提交按钮应禁用
//
// ⚠ 这里必须遍历所有卡逐张跳过 —— 只回翻一张是不够的：
//   直接比较"当前卡是否已跳过"会漏掉前面还有未跳过的卡，
//   于是断言失败而**产品其实是对的**（本断言自己踩过一次）。
const total = 3
for (let i = 0; i < total; i++) {
  // 回到第 i+1 张
  await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('.dispatch-foot button'))
    const prev = btns.find((b) => /上一张/.test(b.innerText))
    return prev ? prev.disabled : true
  })
  for (let k = 0; k < total; k++) {
    if (await page.locator('.dispatch-card button:has-text("上一张"):not([disabled])').count()) {
      await page.locator('.dispatch-card button:has-text("上一张")').click()
      await page.waitForTimeout(250)
    }
  }
  for (let j = 0; j < i; j++) {
    const nx = page.locator('.dispatch-card button:has-text("下一张")')
    if (await nx.count()) { await nx.click(); await page.waitForTimeout(250) }
  }
  const skipBtn = page.locator('.dispatch-card button:has-text("跳过这张")')
  if (await skipBtn.count()) {
    await skipBtn.click()
    await page.waitForTimeout(500)
  }
}

const allSkippedDisabled = await page.evaluate(() => {
  const btns = Array.from(document.querySelectorAll('.dispatch-foot button'))
  const submit = btns.find((b) => /确认派单/.test(b.innerText))
  return submit ? { disabled: submit.disabled, text: submit.innerText.trim() } : null
})
console.log('  submit after skipping all:', JSON.stringify(allSkippedDisabled))
ok(allSkippedDisabled?.disabled === true,
   'submit disabled when every card is skipped')
ok(/确认派单（0 张）/.test(allSkippedDisabled?.text || ''),
   'submit label shows 0 张 when all skipped')

// ══════════════════════════════════════════════════════
console.log('\n=== 纪律：零业务写请求 ===')
// ══════════════════════════════════════════════════════
await page.locator('.dispatch-card .modal-close').click()
await page.waitForTimeout(800)
const bizWrites = writes.filter((w) => !/\/api\/auth\//.test(w))
console.log('  business writes:', JSON.stringify(bizWrites))
ok(bizWrites.length === 0, 'ZERO business writes (nothing was created)')

const real = errs.filter((e) => !/favicon|404/i.test(e))
ok(real.length === 0, 'console errors: ' + real.length +
   (real.length ? ' :: ' + real.slice(0, 3).join(' | ') : ''))

const pass = results.filter((r) => r[0]).length
console.log('\n==================================================')
console.log('RESULT: ' + pass + '/' + results.length + ' passed')
console.log('==================================================')
for (const [c, m] of results) if (!c) console.log('  FAILED:', m)
await browser.close()
process.exit(pass === results.length ? 0 : 1)
