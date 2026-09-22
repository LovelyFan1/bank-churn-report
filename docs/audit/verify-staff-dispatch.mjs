/**
 * 端到端验收：staff 角色 + 批量派单卡片。
 *
 * ⚠ 纪律（本项目曾两次因测试脚本损坏真实数据）：
 *   1) 本脚本**不建单、不改单、不删单** —— 只打开 UI、读 DOM、点开面板看，
 *      最后点"取消"而不点"确认派单"。
 *   2) 断言"写请求 = 0"来证明这一点。
 *   3) 只读的登录 POST 除外（那是鉴权必需的）。
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'https://blessed-ghz-fast-francis.trycloudflare.com'
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })

const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

async function loginAs(username, password = 'Bank@2026') {
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1500, height: 950 } })
  const page = await ctx.newPage()
  const errs = []
  page.on('pageerror', (e) => errs.push('PAGEERROR ' + e.message))
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })

  await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 })
  await page.locator('input[autocomplete="username"]').fill(username)
  await page.locator('input[autocomplete="current-password"]').fill(password)
  await page.locator('button[type="submit"]').click()
  await page.waitForTimeout(2500)

  const totp = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]')
  if (await totp.count()) {
    const fb = page.locator('button:has-text("填入"), button:has-text("使用")')
    if (await fb.count()) { await fb.first().click(); await page.waitForTimeout(700) }
    let code = await totp.first().inputValue().catch(() => '')
    if (!/^\d{4,6}$/.test(code || '')) {
      const m = (await page.evaluate(() => document.body.innerText)).match(/\b(\d{6})\b/)
      if (m) await totp.first().fill(m[1])
    }
    await page.locator('button[type="submit"]').click()
  }
  await page.waitForFunction(() => !location.pathname.includes('/login'), null, { timeout: 30000 }).catch(() => {})
  await page.waitForTimeout(2500)
  return { page, ctx, errs }
}

// ══════════════════════════════════════════════════════
console.log('\n########## 1. staff (wangxiaoyun) ##########')
// ══════════════════════════════════════════════════════
{
  const { page, ctx, errs } = await loginAs('wangxiaoyun')
  console.log('  URL after login:', page.url())

  const nav = await page.evaluate(() => {
    const el = document.querySelector('aside')
    return el ? el.innerText.replace(/\s+/g, ' ').trim() : ''
  })
  console.log('  sidebar:', nav.slice(0, 100))

  ok(nav.includes('挽留工单'), 'staff sees 挽留工单 nav')
  ok(!nav.includes('智能助手'), 'staff does NOT see 智能助手 (no agent:use)')
  ok(!nav.includes('操作审计'), 'staff does NOT see 操作审计')

  // 直接访问助手页应被弹回
  await page.goto(BASE + '/assistant', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(2500)
  ok(!page.url().includes('/assistant'), 'staff direct URL /assistant is redirected away')

  // 工单页：只看到自己的
  await page.goto(BASE + '/work-orders', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(6000)

  const statsText = await page.evaluate(() => document.body.innerText.slice(0, 700))
  const mineLabel = /我的工单/.test(statsText)
  ok(mineLabel, 'staff stats card labeled 「我的工单」 (not 全部工单)')

  // 负责人列应显示姓名而非工号
  const bodyText = await page.evaluate(() => document.body.innerText)
  ok(bodyText.includes('王晓芸'), 'assignee rendered as name (王晓芸)')
  ok(!/wangxiaoyun/.test(bodyText), 'raw username (wangxiaoyun) NOT shown to user')

  // 删除按钮应对 staff 隐藏
  const delCount = await page.locator('button:has-text("✕")').count()
  const editCount = await page.locator('button:has-text("✎")').count()
  console.log('  edit buttons:', editCount, ' delete buttons:', delCount)
  ok(delCount === 0, 'staff sees NO delete button')

  // 详情面板应有 建单人/负责人
  const rows = page.locator('tbody tr')
  if (await rows.count()) {
    await rows.first().click()
    await page.waitForTimeout(1200)
    const det = await page.evaluate(() => document.body.innerText)
    ok(/建单人/.test(det), 'detail panel shows 建单人')
    ok(/负责人/.test(det), 'detail panel shows 负责人')
  }

  const real = errs.filter((e) => !/favicon|404|403/i.test(e))
  ok(real.length === 0, 'staff console errors: ' + real.length +
     (real.length ? ' :: ' + real.slice(0, 2).join(' | ') : ''))
  await ctx.close()
}

// ══════════════════════════════════════════════════════
console.log('\n########## 2. manager (liming) —— 派单卡片 ##########')
// ══════════════════════════════════════════════════════
{
  const { page, ctx, errs } = await loginAs('liming')

  const writes = []
  page.on('request', (r) => {
    const m = r.method().toUpperCase()
    if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m)) {
      writes.push(m + ' ' + new URL(r.url()).pathname)
    }
  })

  await page.goto(BASE + '/customers', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(7000)

  const bodyText = await page.evaluate(() => document.body.innerText)
  ok(/客户流失防控平台|客户名单/.test(bodyText), 'customers page loaded')

  // 勾选前两个可勾选客户
  const checkboxes = page.locator('tbody input[type="checkbox"]:not([disabled])')
  const n = await checkboxes.count()
  console.log('  selectable checkboxes:', n)
  ok(n >= 2, 'at least 2 selectable customers')
  await checkboxes.nth(0).check()
  await checkboxes.nth(1).check()
  await page.waitForTimeout(900)

  const barText = await page.evaluate(() => {
    const el = document.querySelector('.batch-bar')
    return el ? el.innerText.replace(/\s+/g, ' ') : '(no batch bar)'
  })
  console.log('  batch bar:', barText)
  ok(/批量派单/.test(barText), 'batch bar says 「批量派单」')

  // 打开派单卡片
  await page.locator('button:has-text("批量派单")').click()
  await page.waitForTimeout(4000)

  const cardVisible = await page.locator('.dispatch-card').count()
  ok(cardVisible > 0, 'dispatch card opened')

  const cardText = await page.evaluate(() => {
    const el = document.querySelector('.dispatch-card')
    return el ? el.innerText.replace(/\s+/g, ' ').trim() : ''
  })
  console.log('  card content:', cardText.slice(0, 260))

  ok(/1 \/ \d+/.test(cardText), 'card shows progress (1 / N)')
  ok(/负责人/.test(cardText), 'card has 负责人 field')
  ok(/建单理由/.test(cardText), 'card has 建单理由 field')
  ok(/流失概率/.test(cardText) && /触达渠道/.test(cardText), 'card shows customer context')

  // 负责人下拉应有选项且默认已选（预填登录人）
  const selVal = await page.evaluate(() => {
    const s = document.querySelector('.dispatch-card select')
    return s ? { value: s.value, options: s.options.length, text: s.options[s.selectedIndex]?.text || '' } : null
  })
  console.log('  assignee select:', JSON.stringify(selVal))
  ok(selVal && selVal.options > 1, 'assignee dropdown populated (' + (selVal?.options || 0) + ' options)')
  ok(selVal && selVal.value, 'assignee prefilled (not empty) -> ' + selVal?.text)

  // 翻到下一张
  const nextBtn = page.locator('.dispatch-card button:has-text("下一张")')
  if (await nextBtn.count()) {
    await nextBtn.click()
    await page.waitForTimeout(1200)
    const t2 = await page.evaluate(() => document.querySelector('.dispatch-card')?.innerText || '')
    ok(/2 \/ \d+/.test(t2), 'paging works (now on card 2)')
  }

  // 上一张
  await page.locator('.dispatch-card button:has-text("上一张")').click()
  await page.waitForTimeout(1000)
  const t3 = await page.evaluate(() => document.querySelector('.dispatch-card')?.innerText || '')
  ok(/1 \/ \d+/.test(t3), 'back to card 1')

  // ⚠ 关键纪律：点「取消」而不是「确认派单」
  await page.locator('.dispatch-card .modal-close').click()
  await page.waitForTimeout(800)
  ok((await page.locator('.dispatch-card').count()) === 0, 'card closed via cancel')

  const bizWrites = writes.filter((w) => !/\/api\/auth\//.test(w))
  console.log('  business writes:', JSON.stringify(bizWrites))
  ok(bizWrites.length === 0, 'ZERO business writes (nothing created)')

  const real = errs.filter((e) => !/favicon|404/i.test(e))
  ok(real.length === 0, 'manager console errors: ' + real.length +
     (real.length ? ' :: ' + real.slice(0, 2).join(' | ') : ''))
  await ctx.close()
}

// ══════════════════════════════════════════════════════
console.log('\n########## 3. manager creates one order (then we DO write) ##########')
// ══════════════════════════════════════════════════════
{
  const { page, ctx } = await loginAs('liming')
  await page.goto(BASE + '/customers', { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(6000)

  // 打开单建弹窗，检查负责人是下拉且已预填
  const createBtn = page.locator('button:has-text("创建工单")').first()
  if (await createBtn.count()) {
    await createBtn.click()
    await page.waitForTimeout(2500)
    const modal = await page.evaluate(() => {
      const el = document.querySelector('.modal')
      if (!el) return null
      const sels = Array.from(el.querySelectorAll('select'))
      const assigneeSel = sels.find((s) => /负责人/.test(
        s.closest('.form-group')?.innerText || ''))
      return {
        text: el.innerText.replace(/\s+/g, ' ').slice(0, 200),
        hasAssigneeSelect: !!assigneeSel,
        assigneeValue: assigneeSel?.value || '',
        assigneeText: assigneeSel?.options[assigneeSel.selectedIndex]?.text || '',
      }
    })
    console.log('  modal:', JSON.stringify(modal))
    ok(modal && modal.hasAssigneeSelect, 'single-create 负责人 is a SELECT (not free text)')
    ok(modal && !!modal.assigneeValue, 'single-create assignee prefilled -> ' + modal?.assigneeText)
    // 关闭，不提交
    await page.locator('.modal .modal-close').click()
    await page.waitForTimeout(600)
  }
  await ctx.close()
}

const pass = results.filter((r) => r[0]).length
console.log('\n==================================================')
console.log('RESULT: ' + pass + '/' + results.length + ' passed')
console.log('==================================================')
for (const [c, m] of results) if (!c) console.log('  FAILED:', m)
await browser.close()
process.exit(pass === results.length ? 0 : 1)
