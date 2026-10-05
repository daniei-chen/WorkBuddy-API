/**
 * 多语言界面验收（开发工具，不参与构建）。
 *
 * 为什么需要它：i18n 是运行时切换的，静态导出的 HTML 永远是源语言，
 * 抓 HTML 验证不出任何东西。这里用真实浏览器逐语言登录、逐页取
 * `document.body.innerText`，把「译文语言里仍出现汉字」的行报出来——
 * 漏翻、写死文案、以及后端返回的中文都能一眼看到。
 *
 *   node dev/i18n-verify.mjs              # 检查 en / zh-TW / ja / ko
 *   node dev/i18n-verify.mjs en ja        # 只检查指定语言
 *
 * 前置：本机已安装 playwright-core 与 ms-playwright 里的 chromium（脚本会自动
 * 从 %TEMP%\wb-i18n-verify 找依赖，找不到就提示怎么装）。
 */
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const BASE = process.env.WB_BASE || 'http://127.0.0.1:7864';
const USER = process.env.WB_USER || 'admin';
const PASS = process.env.WB_PASS || 'admin123';
const STORAGE_KEY = 'workbuddy-manager:locale';

/** 需要逐页检查的路由（settings 的更新日志页脚注含中文文档内容，单独跳过） */
const PAGES = ['/dashboard', '/accounts', '/tasks', '/keys', '/models', '/playground', '/stats', '/logs', '/security', '/settings'];

/**
 * 页面轮询（心跳刷新）会让 `networkidle` 一直等下去，因此统一用 load + 固定等待。
 * 600ms 足够 React 完成首屏渲染与 localStorage 语言生效后的重渲染。
 */
const SETTLE_MS = 900;

/** 应完全不含汉字的语言（ja 用汉字是正常的，zh-* 本来就该有） */
const NO_HAN = ['en', 'ko'];
/**
 * 简体专用词：只收录**简繁写法不同**的词。
 * 早先误把「模型 / 用量 / 安全」这类简繁同形的词放进来，结果繁体页面被误报一片。
 */
const SIMPLIFIED_MARKERS = [
  '设置', '账号', '密钥', '日志', '任务', '数据', '统计', '暂无',
  '网络', '连接', '过期', '删除', '创建', '密码', '接口', '确认', '刷新页面',
];

const HAN = /[\u3400-\u4dbf\u4e00-\u9fff]/;

async function loadPlaywright() {
  const candidates = [
    path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js'),
    'playwright-core',
  ];
  for (const candidate of candidates) {
    try {
      const spec = candidate.startsWith('/') || /^[A-Za-z]:/.test(candidate)
        ? pathToFileURL(candidate).href
        : candidate;
      const mod = await import(spec);
      return mod.chromium ?? mod.default?.chromium;
    } catch {
      /* 试下一个候选路径 */
    }
  }
  throw new Error('找不到 playwright-core：在 %TEMP%\\wb-i18n-verify 下执行 npm i playwright-core');
}

function chromiumExecutable() {
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  if (!fs.existsSync(root)) return undefined;
  const dir = fs.readdirSync(root).find((d) => d.startsWith('chromium-'));
  if (!dir) return undefined;
  const exe = path.join(root, dir, 'chrome-win64', 'chrome.exe');
  return fs.existsSync(exe) ? exe : undefined;
}

const locales = process.argv.slice(2).filter((a) => !a.startsWith('-'));
const targets = locales.length ? locales : ['en', 'zh-TW', 'ja', 'ko'];

const chromium = await loadPlaywright();
const browser = await chromium.launch({executablePath: chromiumExecutable()});
const findings = [];

for (const locale of targets) {
  const context = await browser.newContext({viewport: {width: 1440, height: 960}});
  const page = await context.newPage();

  // 先落到同源页面，才能写 localStorage（语言是客户端持久化的）
  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.evaluate(([k, v]) => window.localStorage.setItem(k, v), [STORAGE_KEY, locale]);
  await page.reload({waitUntil: 'load'});

  // 通过界面登录，拿到签名 Cookie
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  await Promise.all([
    page.waitForURL(/dashboard/, {timeout: 15000}).catch(() => {}),
    page.click('button[type=submit]'),
  ]);

  /*
   * 账号昵称是**用户数据**（上游带回来的，本来就可能是中文），不翻译，
   * 也就不该被算成漏翻 —— 早先没剔除，任务页与账号页会整片误报，把真正的
   * 漏翻淹掉。先把当前账号池的昵称取出来，逐行剔除后再判汉字。
   */
  const nicknames = await page
    .evaluate(async () => {
      try {
        const res = await fetch('/api/accounts', {credentials: 'same-origin'});
        const data = await res.json();
        const list = Array.isArray(data) ? data : (data.accounts ?? data.items ?? []);
        const names = list.map((a) => a.nickname).filter((n) => typeof n === 'string' && n);
        // 头像位只显示昵称首字（accounts 页的 avatar），一并算作数据
        return [...names, ...names.map((n) => n.slice(0, 1))];
      } catch {
        return [];
      }
    })
    .catch(() => []);
  // 长的先替换，否则「阿」会先把「阿延yan」切碎
  nicknames.sort((a, b) => b.length - a.length);
  /** 剔除昵称：结果行里剩下的汉字才是真漏翻 */
  const stripData = (line) => nicknames.reduce((acc, n) => acc.split(n).join(''), line);

  for (const route of PAGES) {
    await page.goto(`${BASE}${route}`, {waitUntil: 'load', timeout: 20000});
    await page.waitForTimeout(SETTLE_MS);
    const lines = (await page.evaluate(() => document.body.innerText))
      .split('\n')
      .map((l) => l.trim())
      .filter(Boolean);

    for (const line of lines) {
      const text = stripData(line);
      const hasHan = HAN.test(text);
      if (NO_HAN.includes(locale) && hasHan) {
        findings.push({locale, route, kind: 'han', line});
      } else if (locale !== 'zh-CN' && SIMPLIFIED_MARKERS.some((m) => text.includes(m)) && hasHan) {
        findings.push({locale, route, kind: 'simplified', line});
      }
    }
  }

  await page.screenshot({path: path.join(import.meta.dirname, `.verify-${locale}.png`), fullPage: false});
  await context.close();
}

await browser.close();

if (!findings.length) {
  report(`✓ ${targets.join(', ')}：所有页面的可见文案均为目标语言（无残留中文）`);
} else {
  report(`✗ 发现 ${findings.length} 处可疑文案：\n`);
  for (const f of findings) report(`[${f.locale} ${f.route} ${f.kind}] ${f.line.slice(0, 120)}`);
  process.exitCode = 1;
}

// 结果同时写文件：外层管道可能缓冲/截断输出，文件更适合人工复核
function report(text) {
  console.log(text);
  try {
    fs.appendFileSync(path.join(import.meta.dirname, '.verify-report.txt'), `${text}\n`, 'utf8');
  } catch {
    /* 写不进去不影响控制台结果 */
  }
}

// 显式退出：Chromium 子进程有时不随 close() 立刻回收，会拖住事件循环
process.exit(process.exitCode ?? 0);
