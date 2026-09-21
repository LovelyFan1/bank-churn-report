// 只读探针 55：窄视口下类目轴标签是否被 ECharts 自动 interval 跳过
//   category 轴 axisLabel.interval 默认 'auto'：标签重叠时会**隔一个显示**，
//   结果比截断更糟（整簇名消失）。本探针用像素分段统计 x 轴标签段数：
//   期望恰好 5 段（5 个簇），少于 5 即发生了自动隐藏。
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});

// 1024 是 lg 断点临界；1152/1280 为常见窄桌面
for (const w of [1024, 1100, 1152, 1280, 1366]) {
  const page = await browser.newPage({ viewport: { width: w, height: 1000 } });
  await page.route('**/api/**', (r) =>
    r.request().method() !== 'GET' ? r.abort() : r.continue());
  await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(20000);

  const els = await page.$$('div[class*="h-[300px]"]');
  const info = [];
  for (let i = 0; i < els.length; i++) {
    await els[i].scrollIntoViewIfNeeded();
    await page.waitForTimeout(900);
    const b = await els[i].boundingBox();
    await els[i].screenshot({ path: `nv-${w}-${i}.png` });
    info.push({ i, w: Math.round(b?.width) });
  }
  console.log(`w=${w} 图表:`, JSON.stringify(info));
  await page.close();
}

await browser.close();
console.log('done');
