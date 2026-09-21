// 只读探针 60：全站冒烟 —— 确认本次改动未波及其他页面
import { chromium } from 'playwright-core';

const routes = [
  '/dashboard', '/customers', '/work-orders', '/intervention',
  '/assistant', '/clustering', '/eda', '/models',
];

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errs = [];
page.on('pageerror', (e) => errs.push(`[pageerror] ${e.message}`));
page.on('console', (m) => {
  if (m.type() === 'error') errs.push(`[console] ${m.text()}`);
});
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());

let fail = 0;
for (const r of routes) {
  errs.length = 0;
  await page.goto('http://127.0.0.1:5173' + r, { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(r === '/clustering' || r === '/eda' ? 18000 : 4500);
  const title = await page.locator('h1').first().textContent().catch(() => null);
  const ok = errs.length === 0;
  if (!ok) fail++;
  console.log(`${ok ? '✓' : '✗'} ${r.padEnd(16)} h1="${(title || '').trim()}" 错误=${errs.length}`);
  for (const e of errs.slice(0, 3)) console.log('     ' + e);
}

console.log(`\n结果: ${routes.length - fail}/${routes.length} 页无错误`);
await browser.close();
