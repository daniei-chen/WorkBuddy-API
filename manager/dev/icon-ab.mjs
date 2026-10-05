/**
 * 图标参数实机 A/B（开发工具）。
 *
 * 为什么要有它：先前用「实心黑 / 中间灰」像素占比当清晰度指标，结果**指标本身有偏**——
 * `crispEdges` 会直接把抗锯齿的灰像素消掉，指标必然变好，但曲线因此出现锯齿，
 * 观感反而更差。所以这里改成在**真实页面上**用同一套 CSS 渲染多组参数，
 * 同框放大比对，由肉眼判断。
 *
 *   node dev/icon-ab.mjs              # 1.25x 屏
 *   WB_DPR=1 node dev/icon-ab.mjs     # 1x 屏（锯齿最明显）
 *
 * 输出 dev/.ab-<dpr>.png。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7864';
const DPR = process.env.WB_DPR ? Number(process.env.WB_DPR) : 1.25;
const OUT = import.meta.dirname;

async function loadPlaywright() {
  const c = path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js');
  const spec = fs.existsSync(c) ? pathToFileURL(c).href : 'playwright-core';
  const mod = await import(spec);
  return mod.chromium ?? mod.default?.chromium;
}

function exe() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  const dir = fs.existsSync(root) ? fs.readdirSync(root).find((d) => d.startsWith('chromium-')) : null;
  const p = dir ? path.join(root, dir, 'chrome-win64', 'chrome.exe') : '';
  return p && fs.existsSync(p) ? p : undefined;
}

/** 待比对方案的 CSS 覆盖（写进 style 标签，压过 globals.css 的 components 层） */
const VARIANTS = [
  {label: '0 原状 24px / 描边2', css: `
      :is([data-slot='button'],[data-slot$='-trigger']) svg.lucide{width:1.5rem;height:1.5rem;stroke-width:2;shape-rendering:auto}`},
  {label: '1 16px / 描边2', css: `
      :is([data-slot='button'],[data-slot$='-trigger']) svg.lucide{width:1rem;height:1rem;stroke-width:2;shape-rendering:auto}`},
  {label: '2 16px / 描边2.25', css: `
      :is([data-slot='button'],[data-slot$='-trigger']) svg.lucide{width:1rem;height:1rem;stroke-width:2.25;shape-rendering:auto}`},
  {label: '3 16px / 描边2.5', css: `
      :is([data-slot='button'],[data-slot$='-trigger']) svg.lucide{width:1rem;height:1rem;stroke-width:2.5;shape-rendering:auto}`},
  {label: '4 18px / 描边2', css: `
      :is([data-slot='button'],[data-slot$='-trigger']) svg.lucide{width:1.125rem;height:1.125rem;stroke-width:2;shape-rendering:auto}`},
  {label: '5 18px / 描边2.25', css: `
      :is([data-slot='button'],[data-slot$='-trigger']) svg.lucide{width:1.125rem;height:1.125rem;stroke-width:2.25;shape-rendering:auto}`},
];

const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: exe()});
const context = await browser.newContext({viewport: {width: 1680, height: 1000}, deviceScaleFactor: DPR});
const page = await context.newPage();
await page.goto(`${BASE}/login`, {waitUntil: 'load'});
await page.fill('#username', 'admin');
await page.fill('#password', 'admin123');
await Promise.all([page.waitForURL(/dashboard/).catch(() => {}), page.click('button[type=submit]')]);

const shots = [];
for (const [index, variant] of VARIANTS.entries()) {
  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page.waitForTimeout(900);
  // 注入覆盖（后注入的优先，所以每次都清掉上一条）
  await page.evaluate(() => {
    document.getElementById('ab-style')?.remove();
  });
  await page.addStyleTag({content: variant.css, id: 'ab-style'}).catch(async () => {
    await page.evaluate((css) => {
      const el = document.createElement('style');
      el.id = 'ab-style';
      el.textContent = css;
      document.head.appendChild(el);
    }, variant.css);
  });
  await page.waitForTimeout(250);

  const buttons = ['强制重启', '刷新积分', '添加账号'];
  for (const label of buttons) {
    const el = page.getByRole('button', {name: label}).first();
    // 文件名只用序号：label 含斜杠与空格会把路径写坏
    const file = path.join(OUT, `.ab${index}-${buttons.indexOf(label)}.png`);
    await el.screenshot({path: file, scale: 'device'});
    shots.push({index, variant: variant.label, label, file});
  }
}

console.log(JSON.stringify(shots, null, 1));
await context.close();
await browser.close();
process.exit(0);
