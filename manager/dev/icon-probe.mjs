/**
 * 图标渲染探针（开发工具）。
 *
 * 背景：按钮里的图标看起来「发虚」——文字很清楚，图标却是灰蒙蒙的一圈。
 * 常见成因是**描边落在半个像素上**：lucide 图标是 24×24 viewBox、stroke-width 2，
 * 渲染到 14px 时实际描边 = 2 × 14/24 ≈ 1.17px，非整数描边被抗锯齿摊到相邻
 * 两个像素上，视觉上就是「灰 + 糊」。
 *
 * 这个脚本把关键数值量出来：图标盒子尺寸、计算后的 stroke-width、
 * 设备像素比、以及换算到设备像素后的**实际描边宽度**。
 *
 *   node dev/icon-probe.mjs            # 探测各页按钮图标
 *   node dev/icon-probe.mjs --shots    # 额外输出 3 倍放大的按钮截图
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7864';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || 'admin123';
const OUT_DIR = import.meta.dirname;

async function loadPlaywright() {
  const candidates = [
    path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js'),
    'playwright-core',
  ];
  for (const candidate of candidates) {
    try {
      const spec = /^([A-Za-z]:|\/)/.test(candidate) ? pathToFileURL(candidate).href : candidate;
      const mod = await import(spec);
      return mod.chromium ?? mod.default?.chromium;
    } catch {
      /* 试下一个 */
    }
  }
  throw new Error('找不到 playwright-core');
}

function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root).find((d) => d.startsWith('chromium-'));
  const exe = dir ? path.join(root, dir, 'chrome-win64', 'chrome.exe') : '';
  return exe && fs.existsSync(exe) ? exe : undefined;
}

const shots = process.argv.includes('--shots');
/** 设备像素比：Windows 显示缩放 125% / 150% 会让描边落在半个设备像素上 */
const dprArg = process.env.WB_DPR ? Number(process.env.WB_DPR) : 1;
/** `--assert` 用作回归门禁：断言全部按钮图标为 16px，给 CI / 提交前用 */
const assert = process.argv.includes('--assert');
let failures = 0;
const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const context = await browser.newContext({
  viewport: {width: 1680, height: 1000},
  deviceScaleFactor: dprArg,
});
const page = await context.newPage();

await page.goto(`${BASE}/login`, {waitUntil: 'load'});
await page.fill('#username', USER);
await page.fill('#password', PASS);
await Promise.all([
  page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
  page.click('button[type=submit]'),
]);

/**
 * 要探测的按钮：文案 + 路由 + 期望宽度。
 * 期望值以「是否显式写了尺寸类」为准：没写的应落到 16px 默认值，
 * 显式写 `h-3.5 w-3.5` 的（如设置页的「重新拉取」）保持 14px。
 */
const TARGETS = [
  {route: '/accounts', label: '强制重启', expect: 16},
  {route: '/accounts', label: '刷新积分', expect: 16},
  {route: '/accounts', label: '添加账号', expect: 16},
  {route: '/settings', label: '刷新配置与模型', expect: 16},
  {route: '/settings', label: '重新拉取', expect: 14},
];

/**
 * 另外量几个「不该被改动」的位置：
 *   - 显式写了 h-3.5 w-3.5 的标签页图标必须保持 14px（工具类要能覆盖默认值）；
 *   - 底部工具栏的图标不在按钮内，必须保持原样（16px、平滑渲染）。
 * 这两个一起看，才能确认规则「只改了该改的」。
 */
const REGRESSION_CHECKS = [
  {route: '/settings', label: '标签页图标', selector: '[data-slot="tabs-trigger"] svg', expect: 14},
  // 底栏（FloatingDock）的导航图标：必须保持 16px + 平滑渲染，
  // 不能被按钮规则波及（它们不在 button / trigger 里）
  {route: '/accounts', label: '底栏图标', selector: '.fixed.z-40 a[href="/dashboard"] svg', expect: 16},
];

for (const target of TARGETS) {
  await page.goto(`${BASE}${target.route}`, {waitUntil: 'load'});
  await page.waitForTimeout(900);

  // 设置页的「系统更新」标签下有检测更新/刷新，先切过去
  if (target.label === '检测更新' || target.label === '刷新') {
    await page.getByRole('tab', {name: '系统更新'}).click().catch(() => {});
    await page.waitForTimeout(700);
  }

  const button = page.getByRole('button', {name: target.label}).first();
  if (!(await button.count())) {
    console.log(`— ${target.route} 「${target.label}」：未找到按钮（可能无权限或未渲染）`);
    continue;
  }

  const info = await button.evaluate((el) => {
    const svg = el.querySelector('svg');
    if (!svg) return {error: '按钮内没有 svg'};
    const rect = svg.getBoundingClientRect();
    const cs = getComputedStyle(svg);
    const pathBox = svg.getBBox?.();
    return {
      cssWidth: +rect.width.toFixed(3),
      cssHeight: +rect.height.toFixed(3),
      left: +rect.left.toFixed(3),
      top: +rect.top.toFixed(3),
      strokeWidthAttr: svg.getAttribute('stroke-width'),
      strokeWidthComputed: cs.strokeWidth,
      viewBox: svg.getAttribute('viewBox'),
      shapeRendering: cs.shapeRendering,
      classes: svg.getAttribute('class'),
      bbox: pathBox ? {w: +pathBox.width.toFixed(2), h: +pathBox.height.toFixed(2)} : null,
    };
  });

  if (info.error) {
    console.log(`— ${target.label}：${info.error}`);
    continue;
  }

  const dpr = await page.evaluate(() => window.devicePixelRatio);
  const scale = info.cssWidth / 24; // 24 = viewBox 边长
  const effectiveCssStroke = parseFloat(info.strokeWidthComputed) * scale;
  const deviceStroke = effectiveCssStroke * dpr;

  // 回归门禁：按钮图标必须是 16px（见 globals.css 的说明——历史上这里被一个
  // 永远匹配不上的 :not() 选择器坑过，图标一直按 24px 渲染）
  if (assert && Math.abs(info.cssWidth - target.expect) > 0.6) {
    failures += 1;
    console.log(`✗ ${target.label}：按钮图标宽度 ${info.cssWidth}px，应为 ${target.expect}px`);
  }

  console.log(
    `${target.label.padEnd(8)} 盒子=${info.cssWidth}×${info.cssHeight} 位置=(${info.left},${info.top}) ` +
      `viewBox=${info.viewBox} stroke 属性=${info.strokeWidthAttr} 计算值=${info.strokeWidthComputed} ` +
      `→ 等效 ${effectiveCssStroke.toFixed(3)} css px / ${deviceStroke.toFixed(3)} 设备 px` +
      `  dpr=${dpr} shape-rendering=${info.shapeRendering}（期望 ${target.expect}px）`,
  );

  if (shots) {
    // scale: 'device' 按设备像素截，能真实反映用户屏幕上看到的清晰度
    const file = path.join(OUT_DIR, `.icon-dpr${dprArg}-${target.label}.png`);
    await button.screenshot({path: file, scale: 'device'});
    console.log(`    截图：${file}`);
  }
}

console.log('\n--- 回归检查（这些位置的尺寸不应被改动） ---');
for (const check of REGRESSION_CHECKS) {
  await page.goto(`${BASE}${check.route}`, {waitUntil: 'load'});
  await page.waitForTimeout(1000);
  const measured = await page.evaluate((selector) => {
    const svg = document.querySelector(selector);
    if (!svg) return null;
    const cs = getComputedStyle(svg);
    // 把祖先链上的 data-slot 打出来，便于判断规则是被谁命中的
    const chain = [];
    for (let el = svg; el && chain.length < 5; el = el.parentElement) {
      chain.push(`${el.tagName.toLowerCase()}${el.getAttribute('data-slot') ? `[${el.getAttribute('data-slot')}]` : ''}`);
    }
    return {
      width: +svg.getBoundingClientRect().width.toFixed(2),
      shape: cs.shapeRendering,
      stroke: cs.strokeWidth,
      cls: svg.getAttribute('class'),
      chain: chain.join(' < '),
    };
  }, check.selector);
  if (!measured) {
    console.log(`— ${check.label}：选择器未匹配到元素（${check.selector}）`);
    continue;
  }
  const ok = Math.abs(measured.width - check.expect) < 0.6;
  if (assert && !ok) failures += 1;
  console.log(
    `${ok ? '✓' : '✗'} ${check.label.padEnd(6)} 实测 ${measured.width}px（期望 ${check.expect}px）` +
      ` shape-rendering=${measured.shape} stroke=${measured.stroke}`,
  );
  console.log(`          class=${measured.cls}\n          祖先链=${measured.chain}`);
}

await context.close();
await browser.close();

if (assert) {
  console.log(failures ? `\n✗ 图标门禁未通过（${failures} 项）` : '\n✓ 图标门禁通过');
  process.exit(failures ? 1 : 0);
}
process.exit(0);
