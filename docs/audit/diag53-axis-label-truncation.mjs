// 只读探针 53：核实同页另两张图的 x 轴类目名是否真的被 substring 截断
//   「客群指标对比」  c.name?.substring(0, 4)  → 「高流失风险客户」会变「高流失风险」
//   「各簇人数与流失率分布」 c.name?.substring(0, 6) → 变「高流失风险客」
// 用 ECharts 的 getOption 无法从外部拿到（模块作用域），改为截取图表元素后读像素宽度，
// 并与「完整 7 字」应有的宽度对比。
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

// 三张图容器：散点(360) / 对比(300) / 人数分布(300)
const boxes = await page.evaluate(() =>
  [...document.querySelectorAll('div[class*="h-[300px]"]')].map((el) => {
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
  })
);
console.log('300px 容器:', JSON.stringify(boxes));

for (let i = 0; i < boxes.length; i++) {
  const b = boxes[i];
  await page.screenshot({
    path: `axis-chart-${i}.png`,
    clip: { x: b.x, y: b.y, width: b.w, height: b.h },
  });
}

// 顺带把「客群画像」表里完整簇名量出来（作为宽度基准）
const baseline = await page.evaluate(() => {
  const t = [...document.querySelectorAll('table')].find((x) => x.textContent.includes('活跃度'));
  const cell = t?.querySelector('tbody tr:nth-child(2) td');
  return cell ? cell.textContent.trim() : null;
});
console.log('基准完整簇名:', baseline);

await browser.close();
console.log('done');
