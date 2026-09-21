// 只读探针 59：散点图「按流失」着色模式的图例与提示是否也正常
//   该模式图例是 2 项（留存客户/流失客户），置顶后需确认不与轴名/提示相撞
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});

for (const w of [1024, 1366, 1920]) {
  const page = await browser.newPage({ viewport: { width: w, height: 1000 } });
  const errs = [];
  page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  await page.route('**/api/**', (r) =>
    r.request().method() !== 'GET' ? r.abort() : r.continue());
  await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(20000);

  const scatter = await page.$('div[class*="h-[360px]"]');
  await scatter.screenshot({ path: `ch-${w}-cluster.png` });

  // 切到「按流失」
  await page.locator('button:has-text("按流失")').click();
  await page.waitForTimeout(2500);
  await scatter.screenshot({ path: `ch-${w}-churn.png` });

  // 切回「按聚类」再截一次，确认可逆
  await page.locator('button:has-text("按聚类")').click();
  await page.waitForTimeout(2500);
  await scatter.screenshot({ path: `ch-${w}-back.png` });

  console.log(`w=${w} 错误=${errs.length ? JSON.stringify(errs) : '无'}`);
  await page.close();
}

await browser.close();
console.log('done');
