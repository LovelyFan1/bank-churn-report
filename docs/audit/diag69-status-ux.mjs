// 只读探针 69：核实「建单后无法调整状态」
//
// 机制（读码所得）：
//   - 状态按钮在**详情面板**里，面板由点整行触发 toggleDetail(o)
//   - pending  → 只有「▶ 开始处理」
//   - in_progress → 「✓ 标记完成」「✕ 标记流失」
//   - completed / lost → **没有任何状态按钮**（终态）
//
// 本探针只做只读探测（非 GET 全部 abort），验证：
//   A) 点行能否展开面板
//   B) 各状态下按钮是否如代码所述出现
//   C) 「创建工单」弹窗里有没有状态字段（用户可能是在这里找不到）
//   D) 新建的工单初始是什么状态、之后要怎样改
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
const errs = [];
page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());

await page.goto('http://127.0.0.1:5173/work-orders', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(7000);

// ── A/B) 逐行测试展开与按钮 ──
const rows = await page.locator('tbody tr.cursor-pointer').count();
console.log(`工单行数 = ${rows}\n`);

const seen = {};
for (let i = 0; i < Math.min(rows, 12); i++) {
  const row = page.locator('tbody tr.cursor-pointer').nth(i);
  const statusText = (await row.locator('.status-tag').textContent().catch(() => '')).trim();

  // 先收起已展开的
  const open = await page.locator('.detail-panel').count();
  if (open) {
    await page.locator('tbody tr.cursor-pointer').first().click();
    await page.waitForTimeout(300);
    if (await page.locator('.detail-panel').count()) {
      await page.locator('tbody tr.cursor-pointer').first().click();
      await page.waitForTimeout(300);
    }
  }

  await row.click();
  await page.waitForTimeout(700);

  const panel = page.locator('.detail-panel');
  const hasPanel = await panel.count() > 0;
  let buttons = [];
  if (hasPanel) {
    buttons = await panel.locator('button').allTextContents();
    buttons = buttons.map((b) => b.trim()).filter(Boolean);
  }
  if (!seen[statusText]) {
    seen[statusText] = { hasPanel, buttons };
    console.log(`状态「${statusText}」 → 面板=${hasPanel}  按钮=${JSON.stringify(buttons)}`);
  }
}

console.log('\n各状态可用按钮汇总:');
for (const [st, v] of Object.entries(seen)) {
  console.log(`  ${st.padEnd(6)} : ${JSON.stringify(v.buttons)}`);
}

// ── C) 创建弹窗是否有状态字段 ──
console.log('\n── 创建工单弹窗字段 ──');
await page.locator('button:has-text("创建工单"), button:has-text("＋"), button:has-text("新建")')
  .first().click().catch(() => {});
await page.waitForTimeout(1200);
const modal = await page.evaluate(() => {
  const m = document.querySelector('.modal');
  if (!m) return null;
  return {
    title: m.querySelector('h2')?.textContent.trim(),
    labels: [...m.querySelectorAll('label')].map((l) => l.textContent.trim()),
    selects: [...m.querySelectorAll('select')].map((s) => ({
      options: [...s.options].map((o) => o.text.trim()),
      selected: s.options[s.selectedIndex]?.text,
    })),
  };
});
console.log(JSON.stringify(modal, null, 2));

console.log('\n控制台错误:', errs.length ? JSON.stringify(errs) : '无');
await page.screenshot({ path: 'wo-modal.png' });
await browser.close();
