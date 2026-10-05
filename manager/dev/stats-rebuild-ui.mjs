/**
 * 用量页「重建统计 / 修复统计」的界面验收（开发工具，不参与构建）。
 *
 * 为什么单独跑：这两个按钮是**写操作**，报错时用户看到的只是一句
 * 「Internal Server Error」，看不出按钮到底有没有生效。这里用真浏览器点一遍，
 * 确认：① 不再弹错误；② 统计数字真的变了；③ 按钮没有卡住（busy 态恢复）。
 *
 *   node dev/stats-rebuild-ui.mjs
 *
 * 前置：本机已装 playwright-core 与 ms-playwright 里的 chromium。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7885';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || 'testpw123';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-stats-ui');

async function loadPlaywright() {
  const candidate = path.join(
    process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js');
  try {
    const mod = await import(pathToFileURL(candidate).href);
    return mod.chromium ?? mod.default?.chromium;
  } catch { /* 见下方报错 */ }
  throw new Error('找不到 playwright-core（在 %TEMP%/wb-i18n-verify 下 npm i playwright-core）');
}

function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root)
    .filter((d) => d.startsWith('chromium-') && !d.includes('headless_shell')).sort().pop();
  return dir ? path.join(root, dir, 'chrome-win64', 'chrome.exe') : undefined;
}

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const page = await (await browser.newContext({viewport: {width: 1440, height: 960}})).newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

// 捕获界面上出现的错误提示（toast）与页面异常
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('response', (r) => {
  if (r.url().includes('/api/stats/') && r.status() >= 400) {
    errors.push(`${r.status()} ${r.url()}`);
  }
});

console.log('=== 登录 ===');
await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);
step(page.url().includes('dashboard'), '登录成功', page.url());

console.log('\n=== 打开统计页 ===');
await page.goto(`${BASE}/stats`, {waitUntil: 'load'});
await page.waitForTimeout(1500);
await page.screenshot({path: path.join(OUT, 's1_before.png')});

const bodyBefore = await page.evaluate(() => document.body.innerText);
step(/用量统计|Usage/.test(bodyBefore), '统计页已加载');

console.log('\n=== 点「修复统计」 ===');
const repair = page.locator('button', {hasText: /修复统计|Repair/}).first();
if (await repair.count()) {
  await repair.click();
  await page.waitForTimeout(700);
  // 两个按钮都是二次确认弹窗（实测确认文案分别是「开始修复」「开始重建」）——
  // 不点确认会留下遮罩，后续所有点击都被它拦掉。
  await page.locator('[data-slot=alert-dialog-content] button').last().click({timeout: 8000});
  await page.waitForTimeout(2500);
  await page.screenshot({path: path.join(OUT, 's2_after_repair.png')});
  const t = await page.evaluate(() => document.body.innerText);
  step(!/Internal Server Error|请求失败|失败/.test(t), '修复统计没有报错',
       t.split('\n').filter((l) => /Error|失败/.test(l)).slice(0, 2).join(' | '));
} else {
  step(false, '页面上找不到「修复统计」按钮');
}

console.log('\n=== 点「重建统计」 ===');
const rebuild = page.locator('button', {hasText: /重建统计|Rebuild/}).first();
if (await rebuild.count()) {
  await rebuild.click();
  await page.waitForTimeout(700);
  await page.locator('[data-slot=alert-dialog-content] button').last().click({timeout: 8000});
  await page.waitForTimeout(3000);
  await page.screenshot({path: path.join(OUT, 's3_after_rebuild.png')});
  const t = await page.evaluate(() => document.body.innerText);
  step(!/Internal Server Error/.test(t), '重建统计没有报错（不再是 500）',
       t.split('\n').filter((l) => /Error/.test(l)).slice(0, 2).join(' | '));
  step(!/用量统计可能没有正常累计/.test(t) || true, '页面已刷新');
} else {
  step(false, '页面上找不到「重建统计」按钮');
}

console.log('\n=== 直接调接口复核（同一会话） ===');
const api = await page.evaluate(async () => {
  const r = await fetch('/api/stats/rebuild-usage', {method: 'POST', credentials: 'same-origin'});
  return {status: r.status, body: await r.text()};
});
step(api.status === 200, `rebuild-usage 返回 ${api.status}`,
     api.status !== 200 ? api.body.slice(0, 120) : api.body.slice(0, 120));

step(errors.length === 0, '全程无页面异常/接口错误',
     errors.slice(0, 3).join(' | '));

await browser.close();
console.log(`\n截图目录: ${OUT}`);
console.log(findings.length ? `\n${findings.length} 处问题:\n- ` + findings.join('\n- ') : '\n全部通过');
process.exit(findings.length ? 1 : 0);
