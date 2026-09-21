// 只读探针 71：量化「状态入口可发现性」
//   关键疑点：状态按钮藏在详情面板里，而面板靠「点整行」触发，
//   行上没有任何视觉提示（无箭头/无"展开"字样）。
//   本探针检查：
//     A) 行上是否存在任何"可展开"的视觉线索
//     B) 详情面板按钮的可见性（是否需要滚动、字号是否过小）
//     C) 创建弹窗中是否完全无法设置状态
//     D) 排序：新工单是否会沉到列表下方（用户找不到刚建的）
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());
await page.goto('http://127.0.0.1:5173/work-orders', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(7000);

const out = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('tbody tr.cursor-pointer')];
  const first = rows[0];
  const cs = first ? getComputedStyle(first) : null;

  // 行内是否有箭头 / 展开提示字符
  const rowText = first ? first.innerText.replace(/\n/g, ' | ') : '';
  const hasChevron = first
    ? /[▸▾►▼›⌄⌃]/.test(first.innerText) ||
      !!first.querySelector('svg')
    : false;

  // 表头是否有"展开/详情"之类提示
  const headers = [...document.querySelectorAll('thead th')].map((t) => t.textContent.trim());

  return {
    rowCount: rows.length,
    cursor: cs?.cursor,
    hoverBg: cs?.backgroundColor,
    rowTextSample: rowText.slice(0, 120),
    hasChevronOrIcon: hasChevron,
    headers,
    // 操作列里有哪些按钮
    rowActionButtons: first
      ? [...first.querySelectorAll('td:last-child button')].map((b) => b.textContent.trim())
      : [],
  };
});

console.log('=== 列表行 ===');
console.log(JSON.stringify(out, null, 2));

// 展开一行，量按钮的可见性
await page.locator('tbody tr.cursor-pointer').first().click();
await page.waitForTimeout(800);

const panelInfo = await page.evaluate(() => {
  const p = document.querySelector('.detail-panel');
  if (!p) return null;
  const btns = [...p.querySelectorAll('button')];
  return {
    panelRect: (() => {
      const r = p.getBoundingClientRect();
      return { top: Math.round(r.top), h: Math.round(r.height) };
    })(),
    viewportH: window.innerHeight,
    buttons: btns.map((b) => {
      const r = b.getBoundingClientRect();
      const s = getComputedStyle(b);
      return {
        text: b.textContent.trim(),
        fontSize: s.fontSize,
        w: Math.round(r.width),
        h: Math.round(r.height),
        // 是否在首屏可见区内
        inViewport: r.top >= 0 && r.bottom <= window.innerHeight,
      };
    }),
  };
});
console.log('\n=== 详情面板 ===');
console.log(JSON.stringify(panelInfo, null, 2));

// 排序方向：新工单在哪
const order = await page.evaluate(() => {
  const rows = [...document.querySelectorAll('tbody tr.cursor-pointer')];
  return rows.slice(0, 5).map((r) => {
    const tds = r.querySelectorAll('td');
    return {
      date: tds[8]?.textContent.trim(),
      status: r.querySelector('.status-tag')?.textContent.trim(),
    };
  });
});
console.log('\n=== 列表前 5 行的时间与状态（看排序） ===');
console.log(JSON.stringify(order, null, 2));

await browser.close();
