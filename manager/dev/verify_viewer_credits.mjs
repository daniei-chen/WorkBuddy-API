/**
 * 只读账号看到的积分是否与管理员一致（issue #56）。浏览器侧断言。
 *
 * 报告的现象：同一批账号，管理员看到 2,071 / 6,420 / 9,353 / 3,322，只读账号
 * 看到 2,070 / 0 / 0 / 3,323。根因是 `/api/accounts/refresh-credits` 整个
 * admin-only：只读账号页面加载时 403，前端静默吞掉，界面回退到上游 /status 的
 * **快照值**（滞后、可能仍是 0），而 0 还被当成「余额耗尽」标红。
 *
 * 这里分别以两种角色登录、抓下积分列，逐行比对——这是唯一能证明「看到的一样」
 * 的方式（单测只能证明权限放行，证明不了界面真的显示了同一份数字）。
 *
 *   node dev/verify_viewer_credits.mjs     （由 verify_viewer_credits_ui.py 调用）
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7932';
const ADMIN = process.env.WB_ADMIN || 'admin';
const ADMIN_PW = process.env.WB_ADMIN_PW || '';
const VIEWER = process.env.WB_VIEWER || 'looker';
const VIEWER_PW = process.env.WB_VIEWER_PW || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-viewer-credits');

async function loadPlaywright() {
  const explicit = process.env.WB_PLAYWRIGHT;
  const candidates = [
    ...(explicit ? [explicit] : []),
    path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js'),
    'playwright-core',
  ];
  for (const c of candidates) {
    try {
      const mod = await import(c.startsWith('/') || /^[A-Za-z]:/.test(c) ? pathToFileURL(c).href : c);
      return mod.chromium ?? mod.default?.chromium;
    } catch { /* 试下一个 */ }
  }
  throw new Error(`找不到 playwright-core（试过：${candidates.join(', ')}）`);
}

function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root)
    .filter((d) => d.startsWith('chromium-') && !d.includes('headless_shell'))
    .sort().pop();
  if (!dir) return undefined;
  const exe = path.join(root, dir, 'chrome-win64', 'chrome.exe');
  return fs.existsSync(exe) ? exe : undefined;
}

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

/** 以某角色登录，返回 {rows, snapshotBadges, redZeros} */
async function scrape(user, pass, tag) {
  const ctx = await browser.newContext({viewport: {width: 1440, height: 900}});
  const page = await ctx.newPage();
  const stats = [];
  page.on('response', (r) => {
    if (r.url().includes('/api/accounts/refresh-credits')) stats.push(r.status());
  });

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', user);
  await page.fill('#password', pass);
  await Promise.all([
    page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
    page.click('button[type=submit]'),
  ]);

  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  // 等积分列渲染出来（页面加载后会去查实时积分）
  await page.waitForTimeout(2500);
  await page.screenshot({path: path.join(OUT, `${tag}.png`), fullPage: true});

  const data = await page.evaluate(() => {
    const heads = Array.from(document.querySelectorAll('th')).map((e) => e.textContent.trim());
    const idx = heads.findIndex((h) => /积分余额|Credits|크레딧|クレジット/.test(h));
    const rows = [];
    for (const tr of document.querySelectorAll('tbody tr')) {
      const tds = tr.querySelectorAll('td');
      if (!tds.length) continue;
      const name = (tds[0]?.textContent || '').trim().slice(0, 12);
      const cell = idx >= 0 ? tds[idx] : null;
      if (!cell) continue;
      const text = cell.textContent.trim();
      // 数字可能是 1,234 这种形态；取第一个数字串
      const m = text.match(/[\d,]+/);
      rows.push({
        name,
        value: m ? Number(m[0].replace(/,/g, '')) : null,
        raw: text.slice(0, 40),
        isSnapshot: /上游快照|Upstream snapshot|上流スナップショット|업스트림 스냅샷/.test(text),
        isRed: !!cell.querySelector('.text-red-600, .dark\\:text-red-400'),
      });
    }
    const head = document.body.innerText.includes('积分余额')
      ? (document.querySelectorAll('th')[idx]?.textContent || '').trim()
      : '';
    return {rows, head};
  });

  await ctx.close();
  return {...data, apiStatuses: stats};
}

console.log('=== 管理员登录 ===');
const asAdmin = await scrape(ADMIN, ADMIN_PW, 'a1_admin');
step(asAdmin.rows.length > 0, '管理员看到账号行', `行数=${asAdmin.rows.length}`);
const adminRefresh = asAdmin.apiStatuses.filter((s) => s === 200).length;
step(adminRefresh > 0, '管理员的积分查询成功（200）', `状态码=${JSON.stringify(asAdmin.apiStatuses)}`);

console.log('\n=== 只读账号登录 ===');
const asViewer = await scrape(VIEWER, VIEWER_PW, 'a2_viewer');
step(asViewer.rows.length > 0, '只读账号看到账号行', `行数=${asViewer.rows.length}`);
step(asViewer.apiStatuses.every((s) => s === 200),
     '只读账号的积分查询不再 403（issue #56 的根因）',
     `状态码=${JSON.stringify(asViewer.apiStatuses)}`);
// 「故障号」是故意让账单查询失败的那个账号，它本来就该显示快照 —— 排除它
const snapshotRows = asViewer.rows.filter((r) => r.isSnapshot && !r.name.includes('故障号'));
step(snapshotRows.length === 0,
     '只读账号看到的是实时值，不是「上游快照」',
     snapshotRows.length
       ? snapshotRows.map((r) => `${r.name}=${r.raw}`).join(' | ')
       : asViewer.rows.map((r) => `${r.name}=${r.raw}`).join(' | '));

console.log('\n=== 逐行比对两种角色看到的积分 ===');
const byName = (rows) => Object.fromEntries(rows.map((r) => [r.name, r.value]));
const a = byName(asAdmin.rows);
const v = byName(asViewer.rows);
const names = Object.keys(a);
step(names.length > 0 && names.every((n) => n in v), '两种角色看到同一批账号',
     `admin=${JSON.stringify(names)} viewer=${JSON.stringify(Object.keys(v))}`);
const diffs = names.filter((n) => n in v && a[n] !== v[n])
                   .map((n) => `${n}: 管理员=${a[n]} 只读=${v[n]}`);
step(diffs.length === 0, '每个账号的积分在两种角色下一致（报告者的核心诉求）',
     diffs.join('；') || `一致：${names.map((n) => `${n}=${a[n]}`).join(', ')}`);

console.log('\n=== 不该出现「0 分红色告警」 ===');
const falseZeros = asViewer.rows.filter((r) => r.value === 0 && (a[r.name] ?? 0) > 0);
step(falseZeros.length === 0,
     '没有账号在只读视角被误显示成 0（有的账号是 0 分才允许显示 0）',
     falseZeros.map((r) => `${r.name}: 管理员=${a[r.name]} 只读=0`).join('；'));

console.log('\n=== 实时查询失败的账号：如实标注、不误报 ===');
const broken = [...asAdmin.rows, ...asViewer.rows].filter((r) => r.name.includes('故障号'));
step(broken.length > 0, '找到那个账单查询失败的账号', `命中 ${broken.length} 行`);
step(broken.every((r) => r.isSnapshot),
     '取不到实时值 → 明确标成「上游快照」（而不是当成实时值展示）',
     broken.map((r) => `${r.name}=${r.raw}`).join(' | '));
step(broken.every((r) => !r.isRed),
     '快照值不按「余额耗尽」标红（快照说 0 ≠ 确实没积分）',
     broken.map((r) => `${r.name} red=${r.isRed}`).join(' | '));

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
console.log(`截图目录：${OUT}`);
