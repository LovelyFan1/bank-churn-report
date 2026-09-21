// 只读探针 50：量化 /clustering 页面两处文字缺陷的实际几何
//   A) 「客群洞察」卡片标题（簇名）与小字是否重叠
//   B) 「聚类统计」表首列簇名是否被截断
// 全程只发 GET，非 GET 一律 abort —— 不产生任何写入。
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());

await page.goto('http://127.0.0.1:5173/clustering', { waitUntil: 'load', timeout: 60000 });
// 该页要加载 10 万行 + t-SNE，给足时间
await page.waitForTimeout(20000);

const out = await page.evaluate(() => {
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return { x: +r.x.toFixed(1), y: +r.y.toFixed(1), w: +r.width.toFixed(1), h: +r.height.toFixed(1) };
  };
  const overlap = (a, b) => {
    const ix = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
    const iy = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
    return +(ix * iy).toFixed(1);
  };

  const res = { cards: [], statsTable: [], profileTable: [] };

  // ── A) 五个簇卡片 ──
  const cards = [...document.querySelectorAll('.glass-card')].filter(
    (c) => c.querySelector('.text-2xl.font-bold')
  );
  for (const card of cards) {
    const nameEl = [...card.querySelectorAll('span')].find((s) =>
      s.className.includes('font-medium') && s.className.includes('truncate')
    );
    const countEl = card.querySelector('.text-2xl.font-bold');
    if (!nameEl || !countEl) continue;
    const rn = rect(nameEl), rc = rect(countEl);
    res.cards.push({
      text: nameEl.textContent.trim(),
      name: rn,
      count: rc,
      // 正值 = 标题盒与数字盒在垂直方向真的压在一起
      overlapArea: overlap(rn, rc),
      // 标题被 truncate 吃掉的字数（scrollWidth 是完整文本宽度）
      truncated: nameEl.scrollWidth > nameEl.clientWidth,
      scrollW: nameEl.scrollWidth,
      clientW: nameEl.clientWidth,
      hiddenPx: nameEl.scrollWidth - nameEl.clientWidth,
      // 卡片本身够不够宽
      cardW: +card.getBoundingClientRect().width.toFixed(1),
    });
  }

  // ── B) 「聚类统计」表（第二张表）首列 ──
  const statTable = [...document.querySelectorAll('table')].find((t) =>
    t.textContent.includes('分群') && t.textContent.includes('薪资')
  );
  if (statTable) {
    for (const tr of statTable.querySelectorAll('tbody tr')) {
      const cell = tr.querySelector('td');
      const span = cell.querySelector('span.truncate');
      const r = rect(cell);
      res.statsTable.push({
        full: span?.textContent.trim(),
        shown: span?.textContent.trim(),
        cellW: r.w,
        scrollW: span?.scrollWidth,
        clientW: span?.clientWidth,
        clipped: span ? span.scrollWidth > span.clientWidth : null,
        hiddenPx: span ? span.scrollWidth - span.clientWidth : null,
        // 该单元格文本是否溢出表格容器
        overflowsTable: +(
          cell.getBoundingClientRect().right - statTable.getBoundingClientRect().right
        ).toFixed(1),
      });
    }
    res.statTableW = +statTable.getBoundingClientRect().width.toFixed(1);
  }

  // ── C) 「客群画像」表（第三张表）首列 —— 对照，看它是否也截断 ──
  const profileTable = [...document.querySelectorAll('table')].find((t) =>
    t.textContent.includes('活跃度')
  );
  if (profileTable) {
    for (const tr of profileTable.querySelectorAll('tbody tr')) {
      const cell = tr.querySelector('td');
      const span = cell.querySelector('span.font-medium');
      res.profileTable.push({
        full: cell.textContent.trim(),
        cellW: rect(cell).w,
        clipped: cell.scrollWidth > cell.clientWidth,
      });
    }
  }

  return res;
});

console.log(JSON.stringify(out, null, 2));

// 截图存档
await page.screenshot({ path: 'cluster-overlap-before.png', fullPage: false });
await browser.close();
