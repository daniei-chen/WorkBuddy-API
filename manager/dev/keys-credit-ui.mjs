/**
 * 密钥页「积分额度」的界面验收（开发工具，不参与构建）。
 *
 * 为什么要单独跑一遍：后端有单测，但这块改的是**表单与列表渲染** ——
 * 字段没渲染、小数被 input 的 step 卡掉、列表里的超额标记与网关口径不一致，
 * 这些都只有真渲染出来才看得见。
 *
 *   node dev/keys-credit-ui.mjs
 *
 * 前置：本机已装 playwright-core 与 ms-playwright 里的 chromium
 * （与 dev/i18n-verify.mjs 共用同一套依赖）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7885';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || 'testpw123';
const OUT = process.env.WB_SHOTS || path.join(process.env.TEMP || '/tmp', 'wb-keys-credit');

async function loadPlaywright() {
  const candidates = [
    path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js'),
    'playwright-core',
  ];
  for (const c of candidates) {
    try {
      const mod = await import(c.startsWith('/') || /^[A-Za-z]:/.test(c) ? pathToFileURL(c).href : c);
      return mod.chromium ?? mod.default?.chromium;
    } catch { /* 试下一个 */ }
  }
  throw new Error('找不到 playwright-core');
}

function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root).filter((d) => d.startsWith('chromium-') && !d.includes('headless_shell')).sort().pop();
  if (!dir) return undefined;
  const exe = path.join(root, dir, 'chrome-win64', 'chrome.exe');
  return fs.existsSync(exe) ? exe : undefined;
}

fs.mkdirSync(OUT, {recursive: true});
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const ctx = await browser.newContext({viewport: {width: 1440, height: 960}});
const page = await ctx.newPage();

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

console.log('=== 登录 ===');
await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);
step(page.url().includes('dashboard'), '登录成功', page.url());

console.log('\n=== 打开新建密钥弹窗 ===');
await page.goto(`${BASE}/keys`, {waitUntil: 'load'});
await page.waitForTimeout(900);
await page.screenshot({path: path.join(OUT, 'k1_list.png')});

const addBtn = page.locator('button', {hasText: /新建|Create|New/i}).first();
await addBtn.click();
await page.waitForTimeout(900);
await page.screenshot({path: path.join(OUT, 'k2_dialog.png')});

console.log('\n=== 检查积分额度字段 ===');
const allText = await page.evaluate(() => document.body.innerText);
step(/积分额度|Credit quota/.test(allText), '弹窗里有「积分额度」标签');

// 找到该字段对应的 number 输入框（标签后面那个）
const creditInput = page.locator('input[type="number"]').last();
const stepAttr = await creditInput.getAttribute('step');
step(stepAttr === 'any', '积分额度输入框允许小数（step=any）',
     `实际 step=${JSON.stringify(stepAttr)}`);

await creditInput.fill('123.45');
await page.waitForTimeout(300);
const typed = await creditInput.inputValue();
step(typed === '123.45', '可以输入小数', `实际值=${typed}`);
await page.screenshot({path: path.join(OUT, 'k3_filled.png')});

console.log('\n=== 保存并核对列表 ===');
// 密钥名称输入框**没有 type 属性**（HTML 默认即 text，但属性选择器匹配不到），
// 所以按 placeholder 定位——那是它唯一稳定的标识。
await page.locator('input[placeholder*="客服组"], input[placeholder*="Support team"]')
  .first().fill('积分额度验收');
await page.waitForTimeout(200);
await page.locator('button', {hasText: /^创建$|^Create$/}).last().click();
await page.waitForTimeout(1600);
await page.screenshot({path: path.join(OUT, 'k4_after_save.png')});

const listText = await page.evaluate(() => document.body.innerText);
step(/积分额度验收/.test(listText), '新密钥出现在列表里');
step(/123\.45|12345|积分/.test(listText), '列表里能看到积分额度相关信息',
     listText.split('\n').filter((l) => /积分|Credit/.test(l)).slice(0, 3).join(' | '));

console.log('\n=== 一致性：界面超额标记 vs 网关拒绝口径 ===');
// 直接问后端：这把密钥的额度与用量
const apiKey = await page.evaluate(async () => {
  const r = await fetch('/api/keys', {credentials: 'same-origin'});
  const list = await r.json();
  const k = list.find((x) => x.name === '积分额度验收');
  return k ? {id: k.id, quota_credit: k.quota_credit, used_credit: k.used_credit} : null;
});
step(apiKey !== null && apiKey.quota_credit === 123.45,
     '后端存下的是 123.45（没被取整）',
     JSON.stringify(apiKey));

// 清理：删掉验收用的密钥
if (apiKey) {
  await page.evaluate(async (id) => {
    await fetch(`/api/keys/${id}`, {method: 'DELETE', credentials: 'same-origin'});
  }, apiKey.id);
  console.log('  （已清理验收密钥）');
}

await browser.close();
console.log(`\n截图目录: ${OUT}`);
console.log(findings.length ? `\n${findings.length} 处问题:\n- ` + findings.join('\n- ') : '\n全部通过');
process.exit(findings.length ? 1 : 0);
