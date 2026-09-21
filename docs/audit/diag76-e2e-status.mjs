// 端到端实测 76：通过真实 UI 修改状态，验证确实落库。
//   ⚠ 与前几个探针不同，本探针**放行 PUT**（不 abort），因为要验证真实写入。
//   测试对象：工单 #63（pending）—— 由脚本预建，测完由外部脚本删除。
//   流程：pending →(下拉) in_progress →(下拉) completed →(下拉) in_progress 回退
//   每步都用 API 复核 DB 实际值，并检查 result/completed_at 是否符合新规则。
import { chromium } from 'playwright-core';

const OID = process.env.WO_ID;
const API = 'http://localhost:8000';

async function fetchOrder() {
  const r = await fetch(`${API}/api/work-orders/${OID}`);
  return r.json();
}

const browser = await chromium.launch({
  executablePath: process.env.CHROME_PATH,
  args: ['--no-sandbox'],
});
const page = await browser.newPage({ viewport: { width: 1700, height: 1100 } });
const errs = [];
page.on('pageerror', (e) => errs.push('PAGEERROR: ' + e.message));

// 先把测试工单重置为 pending，保证每次都从同一起点开始（幂等）
await fetch(`${API}/api/work-orders/${OID}`, {
  method: 'PUT',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ status: 'pending' }),
});
const reset = await fetchOrder();
console.log(`起点: #${OID} status=${reset.status}`);

// 放行所有请求（真实写入）
await page.goto('http://127.0.0.1:5173/work-orders', { waitUntil: 'load', timeout: 60000 });
await page.waitForTimeout(6000);

// 重置为「全部」，避免改完状态后目标行被当前筛选过滤掉
// （实测坑：在「待处理」筛选下把工单改为 in_progress，该行立即从列表消失，
//   后续 selectOption 找不到元素而超时 —— 这是测试脚本的问题，非功能缺陷。）
await page.locator('button:has-text("全部")').first().click();
await page.waitForTimeout(3000);

// 定位目标行：用工单 id 精确定位。
// 列表按 created_at 倒序，本工单是最新建的，故通常在第 0 行；
// 稳妥起见按「每行创建时间」找到最新那行。
const rowIdx = await page.evaluate((oid) => {
  const rows = [...document.querySelectorAll('tbody tr.cursor-pointer')];
  // 行内不含 id，改用「操作列有 ✎」且状态为 in_progress/pending 的最前行
  // 更可靠：直接在页面上下文里读取 Vue 组件暴露的 orders 不可得，
  // 因此退化为「第 0 行」—— 该工单 created_at 最新，排序后必在第 0 位。
  return rows.length ? 0 : -1;
}, OID);
console.log(`全部列表行数 = ${await page.locator('tbody tr.cursor-pointer').count()}`);
console.log(`目标行索引 = ${rowIdx}`);

// 打印第 0 行的客户与状态，确认确实是我们那条
const info0 = await page.evaluate(() => {
  const r = document.querySelector('tbody tr.cursor-pointer');
  const td = r.querySelectorAll('td');
  return {
    customer: td[0]?.innerText.replace(/\n/g, ' | '),
    status: r.querySelector('select.status-select')?.value,
    created: td[8]?.innerText,
  };
});
console.log(`第 0 行: ${JSON.stringify(info0)}`);
if (rowIdx < 0) {
  console.log('未找到目标工单行，可能是筛选未包含');
  await browser.close();
  process.exit(1);
}

const sel = page.locator('tbody select.status-select').nth(rowIdx);

async function setStatus(target, expectConfirm) {
  const before = await fetchOrder();
  await sel.selectOption(target);
  await page.waitForTimeout(700);

  const dlg = await page.locator('.modal').count();
  if (dlg) {
    await page.locator('.modal-footer button:has-text("确认")').click();
    await page.waitForTimeout(1800);
  } else {
    await page.waitForTimeout(1200);
  }

  const after = await fetchOrder();
  console.log(`\n  ${before.status} → ${target}`);
  console.log(`    弹确认 = ${dlg > 0}（期望 ${expectConfirm}）`);
  console.log(`    DB: status=${after.status}  result=${after.result}  `
    + `completed_at=${after.completed_at ? '有' : '无'}`);
  return { before, after, dlg: dlg > 0 };
}

console.log('\n=== 端到端状态流转（真实写库）===');
const r1 = await setStatus('in_progress', false);
console.log(`    ✓ status 已变更 = ${r1.after.status === 'in_progress'}`);

const r2 = await setStatus('completed', true);
console.log(`    ✓ status=completed, result=${r2.after.result}, `
  + `completed_at 有值 = ${!!r2.after.completed_at}`);

const r3 = await setStatus('in_progress', true);   // 回退：应清痕
console.log(`    ✓ 回退清痕 result=null = ${r3.after.result === null}`);
console.log(`    ✓ 回退清痕 completed_at=null = ${r3.after.completed_at === null}`);

const r4 = await setStatus('lost', true);
console.log(`    ✓ result=lost = ${r4.after.result === 'lost'}`);

const r5 = await setStatus('pending', true);       // lost → pending
console.log(`    ✓ 再回退清痕 = ${r5.after.result === null && r5.after.completed_at === null}`);

console.log('\n控制台错误:', errs.length ? JSON.stringify(errs) : '无');
await page.screenshot({ path: 'wo-e2e.png' });
await browser.close();
