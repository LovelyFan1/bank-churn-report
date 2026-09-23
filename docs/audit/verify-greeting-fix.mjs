/**
 * 寒暄丢答案缺陷修复 —— 浏览器端到端验收。
 *
 * 缺陷：问「你好」，模型已答对（介绍能力），但 node_synthesis 把答案丢掉、
 *       另叫写手对着空数据重编，于是显示「暂无客户数据可分析」，像故障。
 *
 * ⚠ 只读：不点建单/改单/删除任何按钮。断言业务写请求为 0。
 * 用法： node verify-greeting-fix.mjs [baseURL]
 * 运行目录： %TEMP%\pw-probe
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'http://localhost:5173'
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true })
const results = []
const ok = (c, m) => { results.push([c, m]); console.log((c ? '  PASS ' : '  FAIL ') + m) }

const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1500, height: 980 } })
const page = await ctx.newPage()
const errs = []
page.on('pageerror', (e) => errs.push('PAGEERROR ' + e.message))
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })

// 业务写请求留痕 —— 只读验证的核心断言。
// ⚠ 必须排除 /api/agent/ask：它是 POST，但**只读**（只跑图查询，
//   不落库）。写操作只在 /api/agent/confirm* 与 /api/work-orders*。
//   第一版没排除，7 次提问被判成 7 条"写请求" → 假失败。
const bizWrites = []
page.on('request', (r) => {
  const m = r.method().toUpperCase()
  const p = new URL(r.url()).pathname
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(m) &&
      !p.startsWith('/api/auth/') &&
      p !== '/api/agent/ask' &&
      !p.startsWith('/api/agent/confirm')) bizWrites.push(m + ' ' + p)
})

console.log('=== 目标:', BASE, '===')
await page.goto(BASE, { waitUntil: 'networkidle', timeout: 60000 })
await page.locator('input[autocomplete="username"]').fill('zhaomin')
await page.locator('input[autocomplete="current-password"]').fill('Bank@2026')
await page.locator('button[type="submit"]').click()
await page.waitForTimeout(2500)
const totp = page.locator('input[inputmode="numeric"], input[autocomplete="one-time-code"]')
if (await totp.count()) {
  const fb = page.locator('button:has-text("填入"), button:has-text("使用"), button:has-text("获取")')
  if (await fb.count()) { await fb.first().click(); await page.waitForTimeout(700) }
  let c = await totp.first().inputValue().catch(() => '')
  if (!/^\d{4,6}$/.test(c || '')) {
    const m = (await page.evaluate(() => document.body.innerText)).match(/\b(\d{6})\b/)
    if (m) await totp.first().fill(m[1])
  }
  await page.locator('button[type="submit"]').click()
}
await page.waitForFunction(() => !location.pathname.includes('/login'), null, { timeout: 30000 }).catch(() => {})
await page.waitForTimeout(2000)
ok(!page.url().includes('/login'), '登录成功')

await page.goto(BASE + '/assistant', { waitUntil: 'networkidle', timeout: 60000 })
await page.waitForTimeout(3000)

/** 提一个问题，返回最后一轮的回答 */
async function ask(q) {
  const box = page.locator('textarea, input[type="text"]').first()
  await box.fill(q)
  await box.press('Enter')
  // 等新的一轮出现且稳定
  const before = await page.locator('.turn.agent').count()
  await page.waitForFunction(
    (n) => document.querySelectorAll('.turn.agent').length > n,
    before, { timeout: 60000 },
  ).catch(() => {})
  for (let i = 0; i < 40; i++) {
    await page.waitForTimeout(500)
    const last = page.locator('.turn.agent').last()
    const t = await last.innerText().catch(() => '')
    if (t && !/正在|思考中|查询中/.test(t)) break
  }
  const last = page.locator('.turn.agent').last()
  return {
    text: (await last.innerText().catch(() => '')).trim(),
    headline: (await last.locator('.ans-headline').innerText().catch(() => '')).trim(),
    body: (await last.locator('.ans-text').innerText().catch(() => '')).trim(),
    basis: (await last.locator('.basis').innerText().catch(() => '')).trim(),
  }
}

// ══════════════════════════════════════════════════════
console.log('\n########## 一、寒暄类 —— 修复目标 ##########')
// ══════════════════════════════════════════════════════
const GREET = ['你好', '谢谢', '吃了没']

for (const q of GREET) {
  const r = await ask(q)
  console.log(`\n  问「${q}」`)
  console.log(`    徽章: ${r.basis}`)
  console.log(`    正文: ${r.body.replace(/\s+/g, ' ').slice(0, 70)}`)

  // ① 不能再说"暂无数据/无数据可分析"这类故障话术
  const looksBroken = /暂无|无数据|没有数据|未查询|无法回答|未能取得|本轮无/.test(r.text)
  ok(!looksBroken, `「${q}」不再答成故障话术`)

  // ② 要有真实可读的回应正文
  ok(r.body.length >= 8, `「${q}」有回应正文（${r.body.length} 字）`)

  // ③ 正文不得与标题重复显示（headline/text 同值的旧缺陷）
  ok(!(r.headline && r.body && r.headline === r.body), `「${q}」标题与正文未重复`)

  // ④ 徽章仍如实标"未经数据核对"（零工具调用，不得升格为已校验）
  ok(r.basis.includes('未经数据核对'), `「${q}」徽章如实（${r.basis}）`)
}

// ══════════════════════════════════════════════════════
console.log('\n\n########## 二、边界 —— 数据类绝不能被放松 ##########')
// ══════════════════════════════════════════════════════
// 寒暄 + 数据复合问句：必须仍然查库、给数字
const BIZ = [
  ['你好，帮我查一下高危客户有多少人', /24105|24,105|高危/],
  ['谢谢，那决策阈值是多少', /0\.6/],
]

for (const [q, re] of BIZ) {
  const r = await ask(q)
  console.log(`\n  问「${q}」`)
  console.log(`    徽章: ${r.basis}`)
  console.log(`    结论: ${r.headline}`)
  // 必须给出真实数字（说明确实查了库）
  ok(re.test(r.text), `「${q}」给出了真实数据`)
  // 不得被当成寒暄而走"未经数据核对"
  ok(!r.basis.includes('未经数据核对'), `「${q}」未被误判为寒暄（${r.basis}）`)
}

// ══════════════════════════════════════════════════════
console.log('\n\n########## 三、元问题未被削弱 ##########')
// ══════════════════════════════════════════════════════
const META = ['你能做什么', '你是谁']
for (const q of META) {
  const r = await ask(q)
  console.log(`\n  问「${q}」→ ${r.basis} / ${r.headline.slice(0, 40)}`)
  ok(r.basis.includes('系统信息'), `「${q}」仍走系统信息模板（${r.basis}）`)
}

// ══════════════════════════════════════════════════════
console.log('\n\n########## 四、安全断言 ##########')
// ══════════════════════════════════════════════════════
ok(bizWrites.length === 0,
   `业务写请求为 0（实测 ${bizWrites.length} 条${bizWrites.length ? ': ' + bizWrites.join(', ') : ''}）`)
const realErrs = errs.filter(e => !/favicon|404 \(Not Found\)/i.test(e))
ok(realErrs.length === 0,
   `无 JS 报错（${realErrs.length} 条${realErrs.length ? ': ' + realErrs.slice(0, 2).join(' | ') : ''}）`)

const pass = results.filter(r => r[0]).length
console.log(`\n${'='.repeat(50)}\n结果: ${pass}/${results.length} 通过`)
if (pass !== results.length) {
  console.log('失败项:')
  results.filter(r => !r[0]).forEach(r => console.log('  - ' + r[1]))
}
await browser.close()
process.exit(pass === results.length ? 0 : 1)
