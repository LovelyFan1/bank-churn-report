// 只读探针 75：补测「pending → 其它状态」的交互（上一探针因本页无 pending 跳过）
//   用户的实际场景就是：新建工单后想改状态。
//   验证 pending 行：
//     A) 下拉存在且当前值为 pending
//     B) 选 in_progress → 应**无需确认**直接发 PUT
//     C) 选 completed → 应弹确认（结案）
//   全程 abort 写请求，不改数据。
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
const errs = [];
page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));

const puts = [];
await page.route('**/api/**', (r) => {
  const req = r.request();
  if (req.method() !== 'GET') {
    puts.push({ url: req.url().replace('http://127.0.0.1:5173', ''), body: req.postData() });
    return r.abort();   // 保持只读
  }
  return r.continue();
});

// 用「待处理」筛选，确保页面上有 pending 工单
await page.goto('http://127.0.0.1:5173/work-orders?status=pending',
  { waitUntil: 'load', timeout: 60000 }).catch(async () => {
  // 该页筛选状态存在 URL 里，若无则点按钮
  await page.goto('http://127.0.0.1:5173/work-orders', { waitUntil: 'load' });
  await page.locator('button:has-text("待处理")').first().click();
});
await page.waitForTimeout(6000);

// 若 URL 筛选未生效，点「待处理」筛选按钮
const cur = await page.locator('tbody select.status-select').first().inputValue()
  .catch(() => null);
if (cur !== 'pending') {
  await page.locator('button:has-text("待处理")').first().click();
  await page.waitForTimeout(3000);
}

const n = await page.locator('tbody select.status-select').count();
const values = [];
for (let i = 0; i < Math.min(n, 10); i++) {
  values.push(await page.locator('tbody select.status-select').nth(i).inputValue());
}
console.log(`筛选后行数 = ${n}  状态分布 = ${JSON.stringify(values)}`);

if (!values.includes('pending')) {
  console.log('\n⚠ 当前仍无 pending 工单 —— 需先创建一条才能测此场景');
  await browser.close();
  process.exit(0);
}

const idx = values.indexOf('pending');
const sel = page.locator('tbody select.status-select').nth(idx);

// ── B) pending → in_progress（无风险，应免确认）──
puts.length = 0;
await sel.selectOption('in_progress');
await page.waitForTimeout(1000);
const dlgB = await page.locator('.modal').count();
console.log('\n=== B) pending → in_progress ===');
console.log(`  弹确认框 = ${dlgB > 0}（期望 false）`);
console.log(`  写请求 = ${JSON.stringify(puts)}`);
if (dlgB > 0) {
  await page.locator('.modal-footer button:has-text("取消")').click();
  await page.waitForTimeout(500);
}

// 等下拉回滚（因为 abort 会触发回滚）
await page.waitForTimeout(1000);
const back = await page.locator('tbody select.status-select').nth(idx).inputValue();
console.log(`  失败后显示 = ${back}（期望 pending）`);

// ── C) pending → completed（结案，应有确认）──
puts.length = 0;
await page.locator('tbody select.status-select').nth(idx).selectOption('completed');
await page.waitForTimeout(1000);
const dlg = await page.evaluate(() => {
  const m = document.querySelector('.modal');
  return m ? {
    title: m.querySelector('h2')?.textContent.trim(),
    body: m.querySelector('.modal-body p')?.textContent.trim(),
  } : null;
});
console.log('\n=== C) pending → completed ===');
console.log(`  确认弹窗 = ${JSON.stringify(dlg, null, 2)}`);
console.log(`  此时写请求数 = ${puts.length}（期望 0，确认前不该发）`);
if (dlg) await page.locator('.modal-footer button:has-text("取消")').click();

console.log('\n控制台错误(过滤 abort):',
  errs.filter((e) => !/ERR_FAILED|ERR_ABORTED/.test(e)).length || '无');
await browser.close();
