// 只读探针 83：验证前端口径改动的呈现
//   A) Dashboard：ROI 是否带成功率标注；挽留战报是否标注演示数据与不可比
//   B) 干预策略页：是否出现 expected_retained、ROI 公式说明、口径警告
//   C) 两页是否不再出现"挽留成功"这种误导标签
//   D) 无控制台错误
import { chromium } from 'playwright-core';

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
const errs = [];
page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
await page.route('**/api/**', (r) =>
  r.request().method() !== 'GET' ? r.abort() : r.continue());

for (const [route, label] of [['/dashboard', '工作台'], ['/intervention', '干预策略']]) {
  errs.length = 0;
  await page.goto('http://127.0.0.1:5173' + route, { waitUntil: 'load', timeout: 60000 });
  await page.waitForTimeout(9000);

  const txt = await page.evaluate(() => document.body.innerText);
  const checks = {
    含成功率标注: /挽留成功率\s*\d+%|成功率\s*\d+%\s*假设|含\s*\d+%\s*挽留成功率/.test(txt),
    含ROI公式: /ROI\s*=\s*成本比|成本比\s*5/.test(txt),
    标注演示数据: /播种|演示工单|非真实客户回访/.test(txt),
    标注不可比: /不可直接比较|不可比/.test(txt),
    出现分母说明: /分母/.test(txt),
    残留误导_挽留成功: /挽留成功/.test(txt),
    出现期望可挽留: /期望可挽留|期望年可挽留/.test(txt),
  };

  console.log(`\n===== ${route} (${label}) =====`);
  for (const [k, v] of Object.entries(checks)) {
    const good = k.startsWith('残留误导') ? !v : v;
    console.log(`  [${good ? 'PASS' : '注意'}] ${k} = ${v}`);
  }
  console.log(`  控制台错误 = ${errs.length ? JSON.stringify(errs) : '无'}`);

  await page.screenshot({ path: `basis-${route.replace('/', '')}.png`, fullPage: true });
}
await browser.close();
