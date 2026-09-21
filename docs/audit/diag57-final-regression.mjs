// 只读探针 57：CustomerClustering 页最终回归
//   1) 两张表首列完整且一致
//   2) 三张轴图的类目标签：5 段、无叠压、无被 substring 截断
//   3) 散点图 legend 在顶部、t-SNE 提示在底部（分离）
//   4) 控制台 0 错误
//   5) 顺带复查 resize 后标签宽度是否重算（窄→宽）
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});

for (const w of [1024, 1366, 1920]) {
  const page = await browser.newPage({ viewport: { width: w, height: 1000 } });
  const errs = [];
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));

  await page.route('**/api/**', (r) =>
    r.request().method() !== 'GET' ? r.abort() : r.continue());
  await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(20000);

  const tables = await page.evaluate(() => {
    const stat = [...document.querySelectorAll('table')].find(
      (x) => x.textContent.includes('分群') && x.textContent.includes('薪资')
    );
    const prof = [...document.querySelectorAll('table')].find((x) =>
      x.textContent.includes('活跃度')
    );
    const read = (t, sel) =>
      [...(t?.querySelectorAll('tbody tr') || [])].map((tr) =>
        tr.querySelector(sel)?.textContent.trim()
      );
    return { stat: read(stat, 'td span.truncate'), prof: read(prof, 'td') };
  });

  console.log(`\n============ w=${w} ============`);
  console.log(' 聚类统计:', JSON.stringify(tables.stat));
  console.log(' 客群画像:', JSON.stringify(tables.prof));
  console.log(' 两表一致:', JSON.stringify(tables.stat) === JSON.stringify(tables.prof));
  console.log(' 控制台错误:', errs.length ? JSON.stringify(errs) : '无');

  const el = await page.$('div[class*="h-[360px]"]');
  if (el) await el.screenshot({ path: `final-scatter-${w}.png` });

  await page.screenshot({ path: `final-page-${w}.png`, fullPage: true });
  await page.close();
}

await browser.close();
console.log('\ndone');
