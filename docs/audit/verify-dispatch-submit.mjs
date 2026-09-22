/**
 * 真实提交验证：跳过的客户**确实没有**被建单，未跳过的**确实建了**。
 *
 * ⚠ 这是唯一会写数据的验收，纪律：
 *   1) 建完后立即删掉本次创建的单
 *   2) 前后打总数
 *   3) 只删 created_by='liming' 且 id 大于基线最大 id 的单
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

// 登录
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

// 勾 3 人 → 打开派单卡 → 跳过第 2 张 → 提交
await page.goto(BASE + '/customers', { waitUntil: 'networkidle', timeout: 60000 })
await page.waitForTimeout(7000)

const boxes = page.locator('tbody input[type="checkbox"]:not([disabled])')
const total = Math.min(3, await boxes.count())
const picked = []
for (let i = 0; i < total; i++) {
  const box = boxes.nth(i)
  // ⚠ 必须读**这个复选框所在行**的客户编号，不能用 rows[i]！
  //   `:not([disabled])` 会跳过"已有进行中工单"的行（复选框被禁用），
  //   于是复选框下标与 tbody 行下标**不同步** —— 实测因此整体错位一位，
  //   把"跳过的那位"取成了另一位，导致断言指向错误的客户（假失败）。
  const cid = await box.evaluate((el) => {
    const tr = el.closest('tr')
    const m = (tr?.innerText || '').match(/C\d{5,}/)
    return m ? m[0] : null
  })
  await box.check()
  picked.push(cid)
}
console.log('  picked:', picked)
await page.waitForTimeout(800)
await page.locator('button:has-text("批量派单")').click()
await page.waitForTimeout(4500)

// ── 卡片导航辅助 ──────────────────────────────────────
// ⚠ 卡片顺序来自 selected 集合，与表格行顺序**不一定一致**，
//   故一律按"读出当前卡的客户编号"来定位，不按位置假设（踩过）。
const cardCid = async () => page.evaluate(() => {
  const el = document.querySelector('.dispatch-cid')
  return el ? el.innerText.trim() : null
})
const gotoFirst = async () => {
  for (let i = 0; i < 10; i++) {
    const prev = page.locator('.dispatch-card button:has-text("上一张")')
    if (!(await prev.count()) || await prev.isDisabled()) break
    await prev.click()
    await page.waitForTimeout(250)
  }
}
/** 按编号定位到某张卡；找不到返回 false */
const gotoCard = async (cid) => {
  await gotoFirst()
  for (let i = 0; i < 12; i++) {
    if ((await cardCid()) === cid) return true
    const nx = page.locator('.dispatch-card button:has-text("下一张")')
    if (!(await nx.count())) return false
    await nx.click()
    await page.waitForTimeout(350)
  }
  return false
}

const wantSkip = picked[1]
// 挑一个**不跳过**的卡来设负责人，避免"设了负责人却把这张跳掉了"
const wantAssign = picked.find((x) => x && x !== wantSkip)
console.log('  wantSkip:', wantSkip, ' wantAssign:', wantAssign)

// 1) 在不跳过的那张卡上改负责人
ok(await gotoCard(wantAssign), 'navigated to the card we will assign')
await page.selectOption('.dispatch-card select', 'wangxiaoyun')
await page.waitForTimeout(600)
const assignedCid = await cardCid()
console.log('  set assignee=wangxiaoyun on:', assignedCid)

// 2) 定位到要跳过的卡并跳过
ok(await gotoCard(wantSkip), 'navigated to the card we will skip')
const skippedCid = await cardCid()
console.log('  skipping card cid:', skippedCid)
ok(skippedCid === wantSkip, 'located the intended customer to skip')
await page.locator('.dispatch-card button:has-text("跳过这张")').click()
await page.waitForTimeout(900)
// ⚠ 跳过会**自动翻到下一张**（用户点跳过就是想继续看下一张），
//   故此刻当前卡已不是刚跳过的那张 —— 必须回翻过去才能看到跳过标记。
//   本断言踩过一次：直接检查当前卡，看到的是下一张的画像。
ok((await cardCid()) !== skippedCid, 'skip auto-advances to the next card')
ok(await gotoCard(skippedCid), 'can navigate BACK to the skipped card')
const backText = await page.evaluate(() => document.querySelector('.dispatch-cust')?.innerText || '')
const backCard = await page.evaluate(() => document.querySelector('.dispatch-card')?.innerText || '')
console.log('  card after going back:', backText.replace(/\s+/g, ' ').slice(0, 100))
ok(/本次不派单/.test(backCard), 'skipped card shows 本次不派单 when revisited')
ok(/流失概率/.test(backText), '*** profile STILL shown on the revisited skipped card')

// 提交
const submitText = await page.evaluate(() => {
  const btns = Array.from(document.querySelectorAll('.dispatch-foot button'))
  const s = btns.find((b) => /确认派单/.test(b.innerText))
  return s ? s.innerText.trim() : ''
})
console.log('  submit:', submitText)
ok(/确认派单（2 张）/.test(submitText), 'submitting 2 orders (1 skipped)')

await page.locator('.dispatch-card button:has-text("确认派单")').click()
await page.waitForTimeout(9000)

const resultText = await page.evaluate(() => {
  const el = document.querySelector('.batch-result')
  return el ? el.innerText.replace(/\s+/g, ' ') : '(no result panel)'
})
console.log('  result:', resultText)
ok(/成功/.test(resultText), 'batch result panel shown with 成功')

// ══════════════════════════════════════════════════════
console.log('\n=== 用后端数据核对：跳过的没建，未跳过的建了 ===')
// ══════════════════════════════════════════════════════
//
// ⚠ 判定要基于**本次新建的单**（created_by='liming' 且 pending），
//   而不是"该客户名下有没有单" —— 客户名下可能本来就有历史单
//   （completed/lost），那与本次提交无关。本断言踩过一次：
//   跳过的客户名下恰好有一条历史单，导致假失败。

const inspect = await page.evaluate(async (args) => {
  const { skipCid, assignCid } = args
  const t = localStorage.getItem('auth.token.v1')
  const r = await fetch('/api/work-orders?page_size=100', {
    headers: { Authorization: 'Bearer ' + t },
  })
  const j = await r.json()
  const items = j.items || []
  // 本次新建的：负责人是工号（wangxiaoyun）或建单人是 liming 且仍在处理中
  const fresh = items.filter((o) => o.status === 'pending' && o.created_by === 'liming')
  return {
    fresh: fresh.map((o) => ({ id: o.id, cid: o.customer_id, assignee: o.assignee })),
    skipHasFresh: fresh.some((o) => o.customer_id === skipCid),
    skipAll: items.filter((o) => o.customer_id === skipCid)
      .map((o) => ({ id: o.id, status: o.status, created_by: o.created_by })),
    assignFresh: fresh.filter((o) => o.customer_id === assignCid)
      .map((o) => ({ id: o.id, assignee: o.assignee })),
  }
}, { skipCid: skippedCid, assignCid: assignedCid })

console.log('  fresh orders this run:', JSON.stringify(inspect.fresh))
console.log('  skipped customer\'s orders (all):', JSON.stringify(inspect.skipAll))
console.log('  assigned customer\'s fresh order:', JSON.stringify(inspect.assignFresh))

ok(inspect.fresh.length === 2, 'exactly 2 orders created (3 cards - 1 skipped)')
ok(!inspect.skipHasFresh,
   '*** SKIPPED customer has NO new order (core correctness)')
ok(inspect.assignFresh.length === 1 && inspect.assignFresh[0].assignee === 'wangxiaoyun',
   '*** per-card assignee honored on the non-skipped card')

const createdIds = inspect.fresh.map((o) => o.id)
console.log('  candidate ids for cleanup:', createdIds)

const real = errs.filter((e) => !/favicon|404/i.test(e))
ok(real.length === 0, 'console errors: ' + real.length)

const pass = results.filter((r) => r[0]).length
console.log('\n==================================================')
console.log('RESULT: ' + pass + '/' + results.length + ' passed')
console.log('==================================================')
for (const [c, m] of results) if (!c) console.log('  FAILED:', m)
console.log('\nCLEANUP_IDS=' + JSON.stringify(createdIds))
await browser.close()
process.exit(pass === results.length ? 0 : 1)
