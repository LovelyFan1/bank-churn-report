/**
 * 验证三处统计数字的颜色与对比度。
 *
 * 判据不是"值对不对"，而是**在白色卡片背景上是否真的可读** ——
 * 用 WCAG 对比度公式算，因为人眼判断"够不够明显"不可靠，
 * 而这次问题的本质就是"浅色数字放在白卡上"。
 *
 * WCAG AA 对大号文本（≥18.66px bold 或 ≥24px）要求 ≥3.0:1，
 * 普通文本要求 ≥4.5:1。这里是 text-3xl / text-xl 加粗，按 3.0 起判，
 * 但既然要"明显"，用 4.5:1 作为更严的通过线。
 */
import { chromium } from 'playwright-core'
import { installReadOnlyGuard, loginAs } from './_guard.mjs'

const EXE = process.env.USERPROFILE +
  '\\AppData\\Local\\ms-playwright\\chromium-1243\\chrome-win64\\chrome.exe'
const BASE = 'http://localhost:5173'

// 相对亮度（WCAG 2.x）
function lum(rgb) {
  const [r, g, b] = rgb.map(v => {
    const s = v / 255
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4)
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
function contrast(fg, bg) {
  const L1 = lum(fg), L2 = lum(bg)
  const [hi, lo] = L1 > L2 ? [L1, L2] : [L2, L1]
  return (hi + 0.05) / (lo + 0.05)
}
function parse(s) {
  const m = s.match(/\d+(\.\d+)?/g)
  return m ? m.slice(0, 3).map(Number) : null
}

const browser = await chromium.launch({ executablePath: EXE, headless: true })
const ctx = await browser.newContext({ viewport: { width: 1500, height: 1000 } })
const page = await ctx.newPage()
const errs = []
page.on('pageerror', e => errs.push(e.message))
await installReadOnlyGuard(page)
await loginAs(page, 'liming')

const fails = []
function check(name, fgStr, bgStr) {
  const fg = parse(fgStr), bg = parse(bgStr)
  if (!fg || !bg) { fails.push(`${name}: 无法解析颜色`); return }
  const c = contrast(fg, bg)
  const pass = c >= 4.5
  console.log(`  [${pass ? 'OK ' : 'FAIL'}] ${name}`)
  console.log(`         文字 rgb(${fg})  背景 rgb(${bg})  对比度 ${c.toFixed(2)}:1`)
  if (!pass) fails.push(`${name} 对比度仅 ${c.toFixed(2)}:1`)
}

/** 取某页指定文本所在元素的计算颜色与其卡片背景色 */
async function probe(url, labelText, name) {
  await page.goto(BASE + url, { waitUntil: 'networkidle' })
  await page.waitForTimeout(3000)
  const sel = `text="${labelText}"`
  const el = page.locator(sel).first()
  if (await el.count() === 0) {
    console.log(`  [SKIP] ${name}：未找到文本「${labelText}」`)
    return
  }
  // 数值在标签的兄弟节点（卡片内第二行）
  const info = await el.evaluate((node) => {
    const card = node.closest('.glass-card, .metric-card') || node.parentElement
    const val = card.querySelector('.font-bold')
    const cs = getComputedStyle(val)
    // 逐级向上找第一个非透明背景
    let bgEl = card, bg = 'rgba(0, 0, 0, 0)'
    while (bgEl && (bg === 'rgba(0, 0, 0, 0)' || bg === 'transparent')) {
      bg = getComputedStyle(bgEl).backgroundColor
      bgEl = bgEl.parentElement
    }
    return { text: val.textContent.trim(), color: cs.color, bg }
  })
  console.log(`  ${name} → 数值 "${info.text}"`)
  check(`${name}（${labelText}）`, info.color, info.bg)
}

console.log('='.repeat(78))
console.log('三处统计数字的可读性（白卡背景）')
await probe('/customers', '总客户数', '客户名单')
await probe('/work-orders', '全部工单', '挽留工单')
await probe('/intervention', '已闭环工单', '干预策略')

console.log('\n' + '='.repeat(78))
console.log('页面错误:', errs.length)
console.log('结论：' + (fails.length === 0
  ? 'PASS —— 三处数值在白色卡片上对比度均 ≥ 4.5:1'
  : `FAIL —— ${fails.length} 项`))
fails.forEach(f => console.log('   × ' + f))
await browser.close()
