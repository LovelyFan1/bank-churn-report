// 只读探针 56：验证「按槽位宽度自动折行」是否同时解决宽屏丢字与窄屏重叠
//   期望：任何视口下 x 轴都是 5 个独立标签段（interval:0 未被跳过），
//         且标签之间无叠压 —— 宽屏一行、窄屏折两行。
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});

for (const w of [1024, 1100, 1280, 1366, 1600, 1920]) {
  const page = await browser.newPage({ viewport: { width: w, height: 1000 } });
  const errs = [];
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));

  await page.route('**/api/**', (r) =>
    r.request().method() !== 'GET' ? r.abort() : r.continue());
  await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(20000);

  const els = await page.$$('div[class*="h-[300px]"]');
  const dims = [];
  for (let i = 0; i < els.length; i++) {
    await els[i].scrollIntoViewIfNeeded();
    await page.waitForTimeout(800);
    const b = await els[i].boundingBox();
    await els[i].screenshot({ path: `wrap-${w}-${i}.png` });
    dims.push(Math.round(b?.width));
  }
  console.log(`w=${w} 图宽=${JSON.stringify(dims)}  控制台错误=${errs.length ? JSON.stringify(errs) : '无'}`);
  await page.close();
}

await browser.close();
console.log('done');
