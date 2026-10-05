/**
 * 复制链接的「提示与实际一致」验收（issue #57）。浏览器侧断言。
 *
 * 报告的现象：添加账号弹窗里点「复制链接」，提示「已复制到剪贴板」，但粘贴出来
 * 是空的。根因是回退路径 `document.execCommand('copy')` 的**布尔返回值被忽略** ——
 * 被浏览器拒绝时它返回 false，而代码照样 return true。
 *
 * 这里验的是那条不变量，两种环境各跑一遍：
 *   ① 正常环境（有 Clipboard API）：复制后剪贴板里**必须**是那条链接；
 *   ② 模拟非安全上下文（摘掉 `navigator.clipboard`，强制走回退路径）：
 *      **只要提示了成功，剪贴板里就必须有那条链接** —— 提示成功却复制不上就是 bug。
 *
 *   node dev/verify_copy_link.mjs      （由 verify_copy_link_ui.py 调用）
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7942';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || '';

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

const findings = [];
const step = (ok, label, detail = '') => {
  console.log(`  ${ok ? '✓' : '✗'}  ${label}${detail ? `\n       ${detail}` : ''}`);
  if (!ok) findings.push(label + (detail ? `: ${detail}` : ''));
};

const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});

/**
 * 走一次「添加账号 → 复制链接」，返回 {toast, clipboard, link}
 *
 * stripClipboardApi=true 时把 `navigator.clipboard` 摘掉，模拟**非安全上下文**
 * （普通 HTTP 访问就是这种状态），逼代码走 execCommand 回退路径。
 */
async function tryCopy(tag, stripClipboardApi, breakExecCommand) {
  const ctx = await browser.newContext({viewport: {width: 1280, height: 900}});
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: BASE});
  if (stripClipboardApi) {
    await ctx.addInitScript(() => {
      // 覆盖 Navigator 原型上的 getter，让 copyText 走 execCommand 那条路。
      // 同时把**原始描述符**存起来：断言要读剪贴板核对，读的时候再还原，
      // 否则连自己都读不到（第一版就是这样，把「读不到」误当成「复制失败」）。
      const orig = Object.getOwnPropertyDescriptor(Navigator.prototype, 'clipboard');
      window.__restoreClipboard = () => {
        if (orig) Object.defineProperty(Navigator.prototype, 'clipboard', orig);
      };
      Object.defineProperty(Navigator.prototype, 'clipboard', {
        get: () => undefined, configurable: true,
      });
    });
  }
  if (breakExecCommand) {
    // 模拟「回退复制真的被浏览器拒绝」：execCommand 返回 false 而不抛错
    // （非安全上下文、权限被拒、不在用户手势里都是这个形态）。
    // issue #57 的原始代码正是在这里忽略返回值、照样报成功。
    await ctx.addInitScript(() => {
      Object.defineProperty(Document.prototype, 'execCommand', {
        value: () => false, configurable: true, writable: true,
      });
    });
  }
  const page = await ctx.newPage();
  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  await Promise.all([
    page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
    page.click('button[type=submit]'),
  ]);

  // 清空剪贴板，确保后面读到的是这次写入的
  await page.evaluate(() => navigator.clipboard?.writeText?.('__empty__').catch(() => {}));

  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page.locator('button', {hasText: /添加账号|Add account/i}).first().click();
  await page.waitForTimeout(2500);

  // 链接要等 state 拿到才出现（弹窗会去请求授权链接）
  const link = await page.evaluate(() => {
    const a = Array.from(document.querySelectorAll('a'))
      .find((e) => /open\.workbuddy|workbuddy\.ai|codebuddy|oauth|authorize/i.test(e.href || ''));
    return a ? a.href : '';
  });
  if (!link) {
    await page.screenshot({path: path.join(process.env.WB_SHOTS || '.', `${tag}_no_link.png`)});
    await ctx.close();
    return {link: '', toast: '', clipboard: ''};
  }

  const btn = page.locator('button', {hasText: /复制链接|Copy link/i}).first();
  await btn.click();
  await page.waitForTimeout(900);
  await page.screenshot({path: path.join(process.env.WB_SHOTS || '.', `${tag}.png`), fullPage: true});

  const full = await page.evaluate(() => document.body.innerText.replace(/\s+/g, ' '));
  // 只回传**命中的那两句提示**，不要整页文本：整页文本一长就会被截断，
  // 截掉的恰好是末尾的 toast，于是失败时打印出来的证据看起来「没有提示」，
  // 与断言结论矛盾（第一版就是这样，误导了一次排查）。
  const toast = [
    /已复制到剪贴板|Copied to clipboard/.test(full) ? '成功提示' : '',
    /复制失败|Copy failed/.test(full) ? '失败提示' : '',
    /长按选中|Long-press to select/.test(full) ? '手动复制引导' : '',
  ].filter(Boolean).join('+') || '(没有任何复制相关提示)';
  const clipboard = await page.evaluate(async () => {
    // 先把被摘掉的 Clipboard API 还原（复制那一步已经走完回退路径了），
    // 否则这里读不到剪贴板，会把「读不到」误判成「没复制上」
    window.__restoreClipboard?.();
    try {
      return await navigator.clipboard.readText();
    } catch (e) {
      return '(读取剪贴板失败: ' + (e?.name || e) + ')';
    }
  });
  await ctx.close();
  return {link, toast, clipboard};
}

console.log('=== ① 正常环境（有 Clipboard API）===');
const a = await tryCopy('c1_secure', false);
step(!!a.link, '弹窗里拿到了授权链接', a.link.slice(0, 60));
step(a.toast.includes('成功提示'), '提示了「已复制到剪贴板」', a.toast);
step(a.clipboard === a.link, '剪贴板内容与链接一致（提示与实际相符）',
     `剪贴板=${JSON.stringify(a.clipboard.slice(0, 60))}`);

console.log('\n=== ② 非安全上下文（摘掉 Clipboard API，走回退路径）===');
const b = await tryCopy('c2_fallback', true);
step(!!b.link, '弹窗里拿到了授权链接', b.link.slice(0, 60));
const claimedOk = b.toast.includes('成功提示');
const reallyOk = b.clipboard === b.link;
step(!claimedOk || reallyOk,
     '不变量：**提示成功 ⇒ 剪贴板里真的有内容**（issue #57 就是这条被破坏）',
     `提示成功=${claimedOk} 剪贴板正确=${reallyOk} 剪贴板=${JSON.stringify(b.clipboard.slice(0, 60))}`);
if (!claimedOk) {
  step(b.toast.includes('失败提示'),
       '复制没成功时会如实提示失败（而不是报成功）',
       b.toast.slice(0, 120));
}

console.log('\n=== ③ 回退复制被拒绝（execCommand 返回 false）===');
const c = await tryCopy('c3_blocked', true, true);
step(!!c.link, '弹窗里拿到了授权链接', c.link.slice(0, 60));
const cOk = c.toast.includes('成功提示');
const cFail = c.toast.includes('失败提示');
// 这一条是 issue #57 的**精确复现**：复制没成功时，界面不能报成功
step(!cOk, '复制被拒绝时**不报**「已复制到剪贴板」（修复前报的就是它）',
     cOk ? `实际提示：${c.toast.slice(0, 160)}` : '未报成功 ✓');
step(cFail, '复制被拒绝时如实提示失败，并引导手动复制',
     cFail ? '已提示失败 ✓' : `实际提示：${c.toast.slice(0, 160)}`);

await browser.close();

console.log('\n=== 结果 ===');
if (findings.length) {
  console.log(`✗ ${findings.length} 项未通过：`);
  findings.forEach((f) => console.log('   - ' + f));
  process.exit(1);
}
console.log('ALL CHECKS PASSED');
