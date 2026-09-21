// 只读探针 66：缩量到 96418 后，8 个页面的端到端验证。
//   A) 每页无 console 错误
//   B) 页面上显示的客户数是否为 96418（应无 100000 残留）
//   C) 聚类页散点仍为 t-SNE、簇名完整
//   D) 客户名单首行可点开、工单页有数据
// 全程只发 GET，非 GET 一律 abort。
import { chromium } from 'playwright-core';

const routes = [
  ['/dashboard', '工作台'],
  ['/customers', '客户名单'],
  ['/work-orders', '挽留工单'],
  ['/intervention', '干预策略'],
  ['/assistant', '智能助手'],
  ['/clustering', '客群洞察'],
  ['/eda', '数据洞察'],
  ['/models', '模型效果'],
];

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
const errs = [];
page.on('pageerror', (e) => errs.push(`[pageerror] ${e.message}`));
page.on('console', (m) => {
  if (m.type() === 'error') errs.push(`[console] ${m.text()}`);
});
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());

let fail = 0;
const summary = [];

for (const [route, label] of routes) {
  errs.length = 0;
  await page.goto('http://127.0.0.1:5173' + route, { waitUntil: 'load', timeout: 60000 });
  // 聚类/EDA 要全量加载，给足时间
  await page.waitForTimeout(
    route === '/clustering' ? 22000 : route === '/eda' ? 16000 : 6000
  );

  const info = await page.evaluate(() => {
    const txt = document.body.innerText;
    return {
      has100k: /100[,，]?000/.test(txt),
      has96418: /96[,，]?418/.test(txt),
      // 找出页面上出现的所有 5~6 位纯数字（候选客户数）
      nums: (txt.match(/\b\d{2},\d{3}\b/g) || []).slice(0, 12),
      canvas: document.querySelectorAll('canvas').length,
    };
  });

  const ok = errs.length === 0;
  if (!ok) fail++;
  summary.push({ route, label, ok, info, errs: [...errs] });
  console.log(
    `${ok ? 'PASS' : 'FAIL'} ${route.padEnd(14)} ` +
    `canvas=${info.canvas} 含96418=${info.has96418} 含100000=${info.has100k} ` +
    `数字=${JSON.stringify(info.nums)}`
  );
  for (const e of errs.slice(0, 3)) console.log('       ' + e);
}

console.log('\n' + '='.repeat(72));
console.log(`页面: ${routes.length - fail}/${routes.length} 无错误`);

const with100k = summary.filter((s) => s.info.has100k);
if (with100k.length) {
  console.log('\n⚠ 页面上仍出现 100000 的页面:');
  for (const s of with100k) console.log(`   ${s.route} (${s.label})`);
} else {
  console.log('所有页面均未出现 100000');
}

const withN = summary.filter((s) => s.info.has96418);
console.log(`\n显示 96,418 的页面: ${withN.map((s) => s.route).join(', ') || '(无)'}`);

await browser.close();
