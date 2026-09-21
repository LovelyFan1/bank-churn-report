// 只读探针 58：验证 resize 后类目轴标签宽度是否真的重算
//   从宽(1920)拖到窄(1024)：若无 refreshCategoryLabelWidths，标签会溢出重叠
//   从窄(1024)拖到宽(1920)：应恢复单行完整名
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1920, height: 1000 } });
const errs = [];
page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());
await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(20000);

const grab = async (tag) => {
  const els = await page.$$('div[class*="h-[300px]"]');
  for (let i = 0; i < els.length; i++) {
    await els[i].scrollIntoViewIfNeeded();
    await page.waitForTimeout(700);
    const b = await els[i].boundingBox();
    await els[i].screenshot({ path: `rs-${tag}-${i}.png` });
    console.log(`  ${tag} chart${i} w=${Math.round(b.width)}`);
  }
};

console.log('[初始 1920]');
await grab('1920a');

console.log('[缩小到 1024]');
await page.setViewportSize({ width: 1024, height: 1000 });
await page.waitForTimeout(2500);
await grab('1024');

console.log('[放大回 1920]');
await page.setViewportSize({ width: 1920, height: 1000 });
await page.waitForTimeout(2500);
await grab('1920b');

console.log('控制台错误:', errs.length ? JSON.stringify(errs) : '无');
await browser.close();
console.log('done');
