// 只读探针 51：在多视口下截取散点图元素，定位 legend 与 t-SNE 提示文字的重叠
// 全程只发 GET。
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});

// 用户常见窗口宽度：1366 笔记本 / 1440 / 1600 / 1920
const widths = [1280, 1366, 1440, 1600, 1920];

for (const w of widths) {
  const page = await browser.newPage({ viewport: { width: w, height: 1000 } });
  await page.route('**/api/**', (r) =>
    r.request().method() !== 'GET' ? r.abort() : r.continue());

  await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(20000);

  const el = await page.$('div[class*="h-[360px]"]');
  if (!el) {
    console.log(`w=${w}: 未找到散点图容器`);
    await page.close();
    continue;
  }
  await el.screenshot({ path: `cluster-scatter-w${w}.png` });

  // 同时量卡片标题在窄视口下是否会挤压
  const cardInfo = await page.evaluate(() => {
    const cards = [...document.querySelectorAll('.glass-card')].filter((c) =>
      c.querySelector('.text-2xl.font-bold')
    );
    return cards.map((card) => {
      const nameEl = [...card.querySelectorAll('span')].find(
        (s) => s.className.includes('font-medium') && s.className.includes('truncate')
      );
      const countEl = card.querySelector('.text-2xl.font-bold');
      if (!nameEl) return null;
      const rn = nameEl.getBoundingClientRect();
      const rc = countEl.getBoundingClientRect();
      return {
        text: nameEl.textContent.trim(),
        cardW: +card.getBoundingClientRect().width.toFixed(1),
        nameW: +rn.width.toFixed(1),
        hiddenPx: nameEl.scrollWidth - nameEl.clientWidth,
        // 标题底边是否越过数字顶边
        vy: +(rn.bottom - rc.top).toFixed(1),
      };
    }).filter(Boolean);
  });
  console.log(`w=${w}`, JSON.stringify(cardInfo));
  await page.close();
}

console.log('done');
await browser.close();
