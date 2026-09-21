// 只读探针 54：最终验证
//   A) 两张类目轴图的 x 轴标签是否为完整 6/7 字且互不重叠
//   B) 聚类统计表 / 客群画像表 首列一致
//   C) 散点图图例置顶、提示文字留底
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1366, height: 1000 } });
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());
await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(20000);

// A) 三张图截图（散点 360 / 两张 300）
const scatter = await page.$('div[class*="h-[360px]"]');
await scatter.screenshot({ path: 'fin-scatter.png' });

const els = await page.$$('div[class*="h-[300px]"]');
for (let i = 0; i < els.length; i++) {
  await els[i].scrollIntoViewIfNeeded();
  await page.waitForTimeout(1000);
  await els[i].screenshot({ path: `fin-axis-${i}.png` });
}

// B) 两张表的首列对照
const tables = await page.evaluate(() => {
  const pick = (pred) =>
    [...document.querySelectorAll('table')]
      .find(pred)
      ?.['querySelectorAll']('tbody tr');
  const stat = [...document.querySelectorAll('table')].find(
    (x) => x.textContent.includes('分群') && x.textContent.includes('薪资')
  );
  const prof = [...document.querySelectorAll('table')].find((x) =>
    x.textContent.includes('活跃度')
  );
  return {
    stat: [...(stat?.querySelectorAll('tbody tr') || [])].map((tr) =>
      tr.querySelector('td span.truncate')?.textContent.trim()
    ),
    prof: [...(prof?.querySelectorAll('tbody tr') || [])].map((tr) =>
      tr.querySelector('td')?.textContent.trim()
    ),
  };
});
console.log('[B] 聚类统计:', JSON.stringify(tables.stat));
console.log('[B] 客群画像:', JSON.stringify(tables.prof));
console.log(
  '[B] 两表完全一致:',
  JSON.stringify(tables.stat) === JSON.stringify(tables.prof)
);

await page.screenshot({ path: 'fin-page.png', fullPage: true });
await browser.close();
console.log('done');
