// 只读探针 53b：滚动到图表后再截，确保拿到完整 x 轴标签
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());
await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(20000);

const els = await page.$$('div[class*="h-[300px]"]');
console.log('找到', els.length, '个 300px 图表容器');

for (let i = 0; i < els.length; i++) {
  await els[i].scrollIntoViewIfNeeded();
  await page.waitForTimeout(1200);
  const box = await els[i].boundingBox();
  console.log(`#${i} box=`, JSON.stringify(box));
  await els[i].screenshot({ path: `ax2-chart-${i}.png` });
}

await browser.close();
console.log('done');
