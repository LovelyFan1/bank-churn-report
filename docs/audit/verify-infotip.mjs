/**
 * 验收 InfoTip：逐个点开每个页面上的感叹号图标，检查
 *   1) 图标存在且可见
 *   2) 点击后浮层出现
 *   3) 浮层**没有被父容器裁切**（关键风险：表格/卡片有 overflow）
 *   4) 浮层在视口内（不越界）
 *   5) 原文案没有丢失（关键字仍在浮层里）
 * 只读，不写任何数据。
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'https://blessed-ghz-fast-francis.trycloudflare.com'
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 } })
const page = await ctx.newPage()

const errors = []
page.on('pageerror', (e) => errors.push('PAGEERROR ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })

// ── 登录 ──
await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 })
await page.locator('input[autocomplete="username"]').fill('zhaomin')
await page.locator('input[autocomplete="current-password"]').fill('Bank@2026')
await page.locator('button[type="submit"]').click()
await page.waitForTimeout(3000)
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
  await page.waitForTimeout(4000)
}
console.log('登录后:', page.url())

const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

const PAGES = [
  ['/dashboard', '工作台', ['work_orders', '不具备统计意义', '不可直接比较', 'exited=1']],
  ['/intervention', '干预策略', ['推算', '测试集实测', '硬编码比例表', '分母']],
  ['/models', '模型效果', ['决策阈值', '同一判定阈值']],
  ['/assistant', '智能助手', ['不经过模型推算', '确认后执行']],
]

for (const [path, name, keywords] of PAGES) {
  await page.goto(BASE + path, { waitUntil: 'networkidle', timeout: 60000 })
  await page.waitForTimeout(6000)

  const btns = page.locator('.itip-btn')
  const n = await btns.count()
  console.log(`\n=== ${name} (${path})  图标 ${n} 个 ===`)
  if (n === 0) { ok(false, `${name}: 未找到任何 ⓘ 图标`); continue }
  ok(true, `${name}: 找到 ${n} 个图标`)

  for (let i = 0; i < n; i++) {
    const b = btns.nth(i)
    await b.scrollIntoViewIfNeeded().catch(() => {})
    await page.waitForTimeout(250)
    // ⚠ 用 hover 而不是 click：本组件的交互是 hover 打开 / 点击切换。
    //   Playwright 的 click 会先 hover（打开）再 click（又关掉），
    //   于是等 600ms 后浮层已关闭 —— 那是测试写法问题，不是产品问题。
    await b.hover({ timeout: 5000 }).catch(() => {})
    await page.waitForTimeout(600)

    const pop = page.locator('.itip-pop')
    if (!(await pop.count())) { ok(false, `  #${i + 1} 点击后浮层未出现`); continue }

    const info = await page.evaluate(() => {
      const el = document.querySelector('.itip-pop')
      if (!el) return null
      const r = el.getBoundingClientRect()
      // 判断是否被祖先裁剪：逐级比较
      let clipped = false
      let p = el.parentElement
      while (p && p !== document.body) {
        const st = getComputedStyle(p)
        if (/(hidden|auto|scroll|clip)/.test(st.overflow + st.overflowX + st.overflowY)) {
          const pr = p.getBoundingClientRect()
          if (r.top < pr.top - 1 || r.bottom > pr.bottom + 1 || r.left < pr.left - 1 || r.right > pr.right + 1) {
            clipped = true
            break
          }
        }
        p = p.parentElement
      }
      return {
        text: (el.innerText || '').replace(/\s+/g, ' ').trim(),
        w: Math.round(r.width), h: Math.round(r.height),
        inViewport: r.top >= 0 && r.left >= 0 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1,
        clipped,
      }
    })

    if (!info) { ok(false, `  #${i + 1} 读不到浮层`); continue }
    const kw = keywords.filter((k) => info.text.includes(k))
    console.log(`  #${i + 1}: ${info.w}×${info.h}px  视口内=${info.inViewport} 被裁剪=${info.clipped}`)
    console.log(`        "${info.text.slice(0, 70)}..."`)
    ok(info.inViewport, `  #${i + 1} 浮层在视口内`)
    ok(!info.clipped, `  #${i + 1} 未被父容器裁剪`)
    ok(kw.length > 0, `  #${i + 1} 保留原文关键字（${kw.join('、')}）`)
  }
}

const real = errors.filter((e) => !/favicon|404/i.test(e))
ok(real.length === 0, `控制台错误 ${real.length} 条` + (real.length ? ' :: ' + real.slice(0, 2).join(' | ') : ''))

const pass = results.filter((r) => r[0]).length
console.log(`\n=== 结果 ${pass}/${results.length} 通过 ===`)
await browser.close()
process.exit(pass === results.length ? 0 : 1)
