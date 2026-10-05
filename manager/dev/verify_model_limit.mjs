/**
 * 模型受限徽章「显示具体模型名」的验收（用户反馈）。浏览器侧断言。
 *
 * 用户的反馈：徽章只写「模型受限（2 个模型）」，看不出是哪两个，没法换模型、
 * 也没法告诉调用方避开。
 *
 * 这里造三个账号（受限 1 / 2 / 4 个模型），断言：
 *   ① 1 个 → 徽章里就是那个模型名；
 *   ② 2 个 → 两个名字都在徽章里（不再只显示数量）；
 *   ③ 4 个 → 列前两个 + 「+2」，且**悬停里能看到全部四个**（徽章放不下时，
 *      完整清单必须仍在悬停里，否则「+2」就成了信息黑洞）。
 *
 *   node dev/verify_model_limit.mjs     （由 verify_model_limit_ui.py 调用）
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7952';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-model-limit');

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
const ctx = await browser.newContext({viewport: {width: 1500, height: 900}});
const page = await ctx.newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);

await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
await page.waitForTimeout(2000);
await page.screenshot({path: path.join(OUT, 'badges.png'), fullPage: true});

/**
 * 逐个账号行收集「模型受限」徽章的可见文字与 title。
 *
 * 按**行**取，不按整页取：徽章的 tooltip 里只有模型与恢复时间，**不含账号昵称**，
 * 所以必须从行首单元格拿到昵称才能把徽章与账号对上（第一版按 title 里的昵称查，
 * 一个都找不到）。同一行里嵌套的元素可能都满足前缀，取最深的那层。
 */
const badges = await page.evaluate(() => {
  const out = [];
  for (const tr of document.querySelectorAll('tbody tr')) {
    const tds = tr.querySelectorAll('td');
    if (!tds.length) continue;
    const rowText = (tr.textContent || '').trim();
    const cands = [];
    for (const el of tr.querySelectorAll('div,span')) {
      const text = (el.textContent || '').trim();
      if (!text.startsWith('模型受限')) continue;
      const deeper = Array.from(el.querySelectorAll('div,span'))
        .some((d) => (d.textContent || '').trim().startsWith('模型受限'));
      if (!deeper) cands.push({text, title: el.getAttribute('title') || ''});
    }
    if (cands.length) {
      out.push({row: rowText.slice(0, 60), ...cands[0]});
    }
  }
  return out;
});

console.log(`找到 ${badges.length} 个模型受限徽章`);
step(badges.length === 3, '三个账号各有一个模型受限徽章', badges.map((b) => b.text).join(' | '));

// 按**行文本**关联账号（昵称在行里，徽章 tooltip 里没有昵称）
const byName = (n) => badges.find((b) => b.row.includes(n));
const one = byName('单个号');
const two = byName('两个号');
const four = byName('四个号');

// ① 单个受限
step(!!one && one.text.includes('glm-5.2'), '受限 1 个 → 徽章里就是那个模型名',
     one ? one.text : '(没找到)');

// ② 两个受限：两个名字都要在（这是用户反馈的那条）
step(!!two && two.text.includes('glm-5.2') && two.text.includes('deepseek-v4.1-flash'),
     '受限 2 个 → 两个模型名都在徽章里（不再只显示数量）',
     two ? two.text : '(没找到)');
step(!!two && !/（2 个模型）|\(2 models\)/.test(two.text),
     '受限 2 个 → 不再只显示「2 个模型」', two ? two.text : '');

// ③ 四个受限：前两个 + 计数，且悬停里四个都在
step(!!four && four.text.includes('glm-5.2') && four.text.includes('deepseek-v4.1-flash')
     && /\+2\b/.test(four.text),
     '受限 4 个 → 列前两个 + 「+2」', four ? four.text : '(没找到)');
const allFour = ['glm-5.2', 'deepseek-v4.1-flash', 'global:gpt-5.6-sol', 'kimi-k2'];
step(!!four && allFour.every((m) => four.title.includes(m)),
     '受限 4 个 → 悬停里能看到全部四个（「+2」不是信息黑洞）',
     four ? four.title.split('\n').slice(0, 3).join(' / ') : '');

// 悬停里仍带恢复时间（既有行为不能丢）
step(!!two && /恢复|限流中|没有这个模型|until|rate/i.test(two.title),
     '悬停仍带每个模型的原因/恢复时间', two ? two.title.split('\n').slice(1, 3).join(' / ') : '');

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
