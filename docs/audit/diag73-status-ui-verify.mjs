// 只读探针 73：验证方案 C 的前端三项改动
//   A) 列表行有状态下拉，且四个选项齐全
//   B) 行首有展开箭头，点击后旋转
//   C) 详情面板：终态（已完成/已流失）出现「↩ 重新处理」
//   D) 下拉能直接改状态（会发 PUT —— 本探针**拦截并 abort**，只验证交互与弹窗）
//
// ⚠ 只读原则：所有非 GET 请求被 abort，因此状态不会被真正修改。
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
const errs = [];
page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });

// 记录被拦截的写请求（用于证明"下拉确实会发 PUT"），但一律 abort
const blocked = [];
await page.route('**/api/**', (r) => {
  if (r.request().method() !== 'GET') {
    blocked.push({
      method: r.request().method(),
      url: r.request().url().replace('http://127.0.0.1:8000', ''),
      body: r.request().postData(),
    });
    return r.abort();
  }
  return r.continue();
});

await page.goto('http://127.0.0.1:5173/work-orders', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(7000);

// ── A) 列表行状态下拉 ──
const sel = await page.evaluate(() => {
  const s = [...document.querySelectorAll('tbody select.status-select')];
  return {
    count: s.length,
    first: s[0]
      ? {
          value: s[0].value,
          options: [...s[0].options].map((o) => `${o.value}=${o.text.trim()}`),
          className: s[0].className,
        }
      : null,
  };
});
console.log('=== A) 列表行状态下拉 ===');
console.log(`  下拉数量 = ${sel.count}（应等于当前页工单数）`);
console.log(`  首个下拉 = ${JSON.stringify(sel.first, null, 2)}`);

// ── B) 展开箭头 ──
const chev = await page.evaluate(() => {
  const c = document.querySelector('tbody .chev');
  if (!c) return null;
  const cs = getComputedStyle(c);
  return { text: c.textContent, transform: cs.transform, color: cs.color };
});
console.log('\n=== B) 展开箭头 ===');
console.log(`  未展开时: ${JSON.stringify(chev)}`);

// 点行展开，看箭头是否旋转
await page.locator('tbody tr.cursor-pointer').first().click();
await page.waitForTimeout(900);
const chevOpen = await page.evaluate(() => {
  const tr = document.querySelector('tbody tr.expanded');
  if (!tr) return null;
  const c = tr.querySelector('.chev');
  return {
    rowExpanded: true,
    chevTransform: getComputedStyle(c).transform,
    panelPresent: !!document.querySelector('.detail-panel'),
  };
});
console.log(`  展开后: ${JSON.stringify(chevOpen)}`);

// ── C) 详情面板按钮（含终态）──
const panels = {};
const rows = await page.locator('tbody tr.cursor-pointer').count();
for (let i = 0; i < Math.min(rows, 20); i++) {
  const row = page.locator('tbody tr.cursor-pointer').nth(i);
  const st = (await row.locator('select.status-select').inputValue().catch(() => '')).trim();
  if (panels[st]) continue;

  // 收起上一个
  const openRow = page.locator('tbody tr.expanded');
  if (await openRow.count()) {
    await openRow.first().click();
    await page.waitForTimeout(300);
  }
  await row.click();
  await page.waitForTimeout(700);
  const btns = await page.locator('.detail-panel button').allTextContents();
  const hasSelect = await page.locator('.detail-panel select.status-select').count();
  panels[st] = { buttons: btns.map((b) => b.trim()), hasSelect: hasSelect > 0 };
  if (Object.keys(panels).length >= 4) break;
}

console.log('\n=== C) 详情面板按钮（按状态） ===');
for (const [st, v] of Object.entries(panels)) {
  console.log(`  ${st.padEnd(12)} : ${JSON.stringify(v.buttons)}  含下拉=${v.hasSelect}`);
}

// ── D) 下拉改状态 → 应弹确认框（写请求被 abort）──
console.log('\n=== D) 列表下拉改状态 ===');
blocked.length = 0;
const first = page.locator('tbody select.status-select').first();
const cur = await first.inputValue();
// 选一个与当前不同的状态
const target = cur === 'completed' ? 'lost' : 'completed';
await first.selectOption(target);
await page.waitForTimeout(1000);

const dlg = await page.evaluate(() => {
  const m = document.querySelector('.modal');
  return m ? {
    title: m.querySelector('h2')?.textContent.trim(),
    body: m.querySelector('.modal-body p')?.textContent.trim(),
    buttons: [...m.querySelectorAll('.modal-footer button')].map((b) => b.textContent.trim()),
  } : null;
});
console.log(`  当前 ${cur} → 选择 ${target}`);
console.log(`  确认弹窗 = ${JSON.stringify(dlg, null, 2)}`);
console.log(`  被拦截的写请求 = ${JSON.stringify(blocked)}`);

// 关掉弹窗
if (dlg) {
  await page.locator('.modal-footer button:has-text("取消")').click();
  await page.waitForTimeout(400);
}

console.log('\n控制台错误:', errs.length ? JSON.stringify(errs) : '无');
await page.screenshot({ path: 'wo-new-ui.png' });
await browser.close();
