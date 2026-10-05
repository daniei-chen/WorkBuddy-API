/**
 * 全页截图（开发工具）：改动全局样式后用来肉眼扫一遍有没有布局回归。
 *
 *   node dev/ui-shots.mjs            # 默认导出 accounts / settings / dashboard
 *   node dev/ui-shots.mjs /logs      # 指定路由
 *
 * 输出 dev/.ui-<name>-dpr<d>.png（已 gitignore）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7864';
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

const routes = process.argv.slice(2).filter((a) => a.startsWith('/'));
const targets = routes.length ? routes : ['/accounts', '/settings', '/dashboard'];
const dpr = process.env.WB_DPR ? Number(process.env.WB_DPR) : 1.25;

const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: exe()});
const context = await browser.newContext({viewport: {width: 1680, height: 1050}, deviceScaleFactor: dpr});
const page = await context.newPage();

await page.goto(`${BASE}/login`, {waitUntil: 'load'});
await page.fill('#username', 'admin');
await page.fill('#password', 'admin123');
await Promise.all([page.waitForURL(/dashboard/).catch(() => {}), page.click('button[type=submit]')]);

for (const route of targets) {
  await page.goto(`${BASE}${route}`, {waitUntil: 'load'});
  await page.waitForTimeout(1200);
  const name = route.replace(/\//g, '') || 'home';
  const file = path.join(OUT, `.ui-${name}-dpr${dpr}.png`);
  await page.screenshot({path: file, scale: 'device'});
  console.log(`${route} → ${file}`);
}

await context.close();
await browser.close();
process.exit(0);
