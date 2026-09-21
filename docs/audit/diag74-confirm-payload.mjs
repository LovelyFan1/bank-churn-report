// 只读探针 74：验证「确认后发出的 PUT 内容」与「失败回滚」
//   1) 点确认 → 应发出 PUT，body 含 status 与 result
//   2) 回退到非结案态时，result 必须显式送 null（清空结案痕迹）
//   3) 请求失败（本探针 abort 模拟）后，下拉应回滚到原值，不显示假状态
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
const errs = [];
page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });

let mode = 'abort';           // abort | pass
const puts = [];
await page.route('**/api/**', (r) => {
  const req = r.request();
  if (req.method() !== 'GET') {
    puts.push({ method: req.method(), url: req.url(), body: req.postData() });
    if (mode === 'abort') return r.abort();
  }
  return r.continue();
});

await page.goto('http://127.0.0.1:5173/work-orders', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(7000);

const sel = page.locator('tbody select.status-select').first();
const original = await sel.inputValue();
console.log(`首个工单原状态 = ${original}\n`);

// ── 场景 1：completed → in_progress（回退，应显式送 result:null）──
// 先找一个 completed 的行
const rows = await page.locator('tbody tr.cursor-pointer').count();
let target = null;
for (let i = 0; i < rows; i++) {
  const s = page.locator('tbody select.status-select').nth(i);
  if ((await s.inputValue()) === 'completed') { target = s; break; }
}
if (!target) { console.log('本页无 completed 工单，改用首个'); target = sel; }

const from = await target.inputValue();
puts.length = 0;
await target.selectOption('in_progress');
await page.waitForTimeout(600);
// 点确认
const hasDlg = await page.locator('.modal').count();
if (hasDlg) {
  await page.locator('.modal-footer button:has-text("确认")').click();
  await page.waitForTimeout(1200);
}
console.log('=== 场景 1：回退到 in_progress ===');
console.log(`  ${from} → in_progress`);
console.log(`  发生的写请求: ${JSON.stringify(puts, null, 2)}`);
const put1 = puts.find((p) => p.method === 'PUT');
console.log(`  → result 是否显式送 null: ${put1 ? /"result":\s*null/.test(put1.body) : 'N/A'}`);

// ── 场景 2：失败回滚 ──
await page.waitForTimeout(800);
const afterFail = await target.inputValue();
console.log(`\n=== 场景 2：请求失败后的回滚 ===`);
console.log(`  下拉当前显示 = ${afterFail}（应回到 ${from}）`);
console.log(`  回滚正确 = ${afterFail === from}`);

const toast = await page.locator('.toast').textContent().catch(() => null);
console.log(`  错误提示 = ${toast ? toast.trim() : '(无)'}`);

// ── 场景 3：pending → in_progress 不应有确认框（无风险流转）──
puts.length = 0;
let pend = null;
for (let i = 0; i < rows; i++) {
  const s = page.locator('tbody select.status-select').nth(i);
  if ((await s.inputValue()) === 'pending') { pend = s; break; }
}
if (pend) {
  await pend.selectOption('in_progress');
  await page.waitForTimeout(900);
  const dlg = await page.locator('.modal').count();
  console.log(`\n=== 场景 3：pending → in_progress（应无需确认）===`);
  console.log(`  弹确认框 = ${dlg > 0}（期望 false）`);
  console.log(`  直接发出 PUT = ${puts.some((p) => p.method === 'PUT')}（期望 true）`);
  if (dlg > 0) await page.locator('.modal-footer button:has-text("取消")').click();
} else {
  console.log('\n=== 场景 3：本页无 pending 工单，跳过 ===');
}

console.log('\n控制台错误:', errs.length ? JSON.stringify(errs) : '无');
await browser.close();
