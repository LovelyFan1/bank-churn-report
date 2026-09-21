// 只读探针 52：修复后验证（截图 + DOM 取值；像素分析交给 Python）
//   A) 散点图 legend 是否已与右下角 t-SNE 提示文字分离
//   B) 「聚类统计」表首列是否显示完整簇名
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});

for (const w of [1280, 1366, 1440, 1600, 1920]) {
  const page = await browser.newPage({ viewport: { width: w, height: 1000 } });
  await page.route('**/api/**', (r) =>
    r.request().method() !== 'GET' ? r.abort() : r.continue());
  await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(20000);

  console.log(`\n================ w=${w} ================`);

  // ── B) 聚类统计表首列 ──
  const table = await page.evaluate(() => {
    const t = [...document.querySelectorAll('table')].find(
      (x) => x.textContent.includes('分群') && x.textContent.includes('薪资')
    );
    if (!t) return null;
    return [...t.querySelectorAll('tbody tr')].map((tr) => {
      const span = tr.querySelector('td span.truncate');
      return {
        text: span?.textContent.trim(),
        title: span?.getAttribute('title'),
        hiddenPx: span ? span.scrollWidth - span.clientWidth : null,
      };
    });
  });
  console.log('[B] 聚类统计表首列:');
  for (const r of table || []) {
    console.log(
      `    "${r.text}"  ${[...(r.text || '')].length}字  截断=${r.hiddenPx > 0 ? '是(+' + r.hiddenPx + 'px)' : '否'}`
    );
  }

  // ── C) 客群画像表（对照）──
  const ptable = await page.evaluate(() => {
    const t = [...document.querySelectorAll('table')].find((x) => x.textContent.includes('活跃度'));
    if (!t) return null;
    return [...t.querySelectorAll('tbody tr')].map((tr) => tr.querySelector('td').textContent.trim());
  });
  console.log('[C] 客群画像表首列:', JSON.stringify(ptable));

  // ── A) 散点图截图，供 Python 做像素列段分析 ──
  const el = await page.$('div[class*="h-[360px]"]');
  if (el) await el.screenshot({ path: `fix-scatter-w${w}.png` });

  await page.screenshot({ path: `fix-page-w${w}.png` });
  await page.close();
}

await browser.close();
console.log('\ndone');
