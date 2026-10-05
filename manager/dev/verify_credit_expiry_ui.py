"""积分到期倒计时的端到端验收（开发工具，不参与构建）。

为什么必须真起一遍：解析逻辑有单测，但「到期时间能不能到用户眼前」这条链路上
还有三段单测覆盖不到 —— 账单响应 → credits 缓存 → 接口 → 前端渲染。任何一段
把字段丢掉，界面只是**安静地不显示倒计时**，没有报错、没有异常，测试全绿。

做法：起一个返回 `CycleEndTime` 的**假腾讯账单服务**，把管理端指过去，再用真
浏览器打开账号页与首页，断言倒计时文案真的出现在页面上。

    python dev/verify_credit_expiry_ui.py            # 起服务并截图
    python dev/verify_credit_expiry_ui.py --check    # 起服务 + 断言（CI 友好）

数据落在 dev/.credit-expiry/（已 gitignore）。截图输出到 dev/.credit-shots/。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.credit-expiry'
SHOTS = REPO / 'dev' / '.credit-shots'
UPSTREAM_PORT = 7893
BILLING_PORT = 7894
MANAGER_PORT = 7895
ADMIN_PW = 'credit-pass-123'

CN = timezone(timedelta(hours=8))


def cn_stamp(seconds_from_now: float) -> str:
    """腾讯形态的到期串：UTC+8 墙钟。"""
    return datetime.fromtimestamp(time.time() + seconds_from_now, CN).strftime('%Y-%m-%d %H:%M:%S')


# 三个账号，覆盖「正常 / 快到期 / 无到期信息」三种展示形态。
# 关键：到期时间按 UTC+8 墙钟给，管理端必须换算正确 —— 页面上的日期一旦差 8 小时，
# 用户拿它跟腾讯官网对账时会发现对不上。
ACCOUNTS = [
    # 分钟档（最后一分钟外的最近一档）：只有它会用到秒级时钟
    {'uid': 'u-min', 'nickname': '快到点了',
     'packages': [{'size': 1000, 'remain': 400, 'used': 600, 'end': cn_stamp(100)}]},
    # 小时档：倒计时按小时显示（<1 天）
    {'uid': 'u-hours', 'nickname': '几小时后到期',
     'packages': [{'size': 1000, 'remain': 800, 'used': 200, 'end': cn_stamp(5 * 3600)}]},
    # 天档：2 天多 → 「2 天后到期」
    {'uid': 'u-soon', 'nickname': '快到期号',
     'packages': [{'size': 3000, 'remain': 1200, 'used': 1800, 'end': cn_stamp(2 * 86400 + 3600)}]},
    # 天档：一个多月
    {'uid': 'u-far', 'nickname': '还早的号',
     'packages': [{'size': 5000, 'remain': 4200, 'used': 800, 'end': cn_stamp(45 * 86400)}]},
    # 无到期时间（永不过期的套餐）→ 不显示倒计时
    {'uid': 'u-noexp', 'nickname': '无到期号',
     'packages': [{'size': 500, 'remain': 500, 'used': 0, 'end': None}]},
    # 多套餐：验证悬停明细里逐条列出，且胶囊按**最早**那笔算倒计时
    {'uid': 'u-multi', 'nickname': '多套餐号',
     'packages': [
         {'size': 2000, 'remain': 1500, 'used': 500, 'end': cn_stamp(20 * 86400)},
         {'size': 1000, 'remain': 300, 'used': 700, 'end': cn_stamp(9 * 86400)},
     ]},
]


def billing_payload(uid: str | None = None) -> dict:
    """照真实账单响应结构：Response.Data.Accounts。

    按 X-User-Id 只回该账号的套餐——真实账单接口是**按调用者身份**返回的。
    全量返回的话，三个账号会显示同一份数据，串号类 bug（例如缓存没按 uid
    隔离）在界面上就看不出来了。
    """
    picked = [a for a in ACCOUNTS if a['uid'] == uid] if uid else ACCOUNTS
    accounts = []
    for acc in picked or ACCOUNTS:
        for p in acc['packages']:
            item = {
                'PackageName': '套餐',
                'CycleCapacitySize': p['size'],
                'CycleCapacityRemain': p['remain'],
                'CycleCapacityUsed': p['used'],
                'PackageEndTime': '',  # 真实响应里恒为空：认它就会永远不显示倒计时
            }
            if p['end']:
                item['CycleEndTime'] = p['end']
            accounts.append(item)
    return {'code': 0, 'data': {'Response': {'Data': {'Accounts': accounts}}}}


class Billing(BaseHTTPRequestHandler):
    """假腾讯 billing 域：只认 get-user-resource。"""

    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        raw = self.rfile.read(n) if n else b''
        path = self.path.split('?')[0]
        # 记录请求体，供校验「时间窗过滤串」是否按 UTC+8 发出
        Billing.last_request = {'path': path, 'body': raw.decode('utf-8', 'replace'),
                                'headers': dict(self.headers)}
        if 'get-user-resource' in path:
            self._json(billing_payload(self.headers.get('X-User-Id')))
        else:
            self._json({'code': 0, 'data': {}})


class Upstream(BaseHTTPRequestHandler):
    """假 workbuddy2api：/status 里带上三个账号。"""

    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/healthz':
            self._json({'healthy': 3, 'total': 3})
        elif path == '/status':
            pool = [
                {'uid': a['uid'], 'nickname': a['nickname'], 'realm': 'cn',
                 'healthy': True, 'disabled': False, 'cooling': False,
                 'success_count': 10, 'err_total': 0,
                 'credits': sum(p['remain'] for p in a['packages'])}
                for a in ACCOUNTS
            ]
            self._json({
                'accounts': pool, 'total': len(pool), 'healthy': len(pool),
                'cooling': 0, 'disabled': 0, 'in_flight_full': 0,
                'redis_mode': 'noop', 'sticky_sessions': 0,
                'realm_totals': {
                    'cn': {'total': len(pool), 'healthy': len(pool), 'cooling': 0,
                           'disabled': 0, 'in_flight_full': 0},
                    'global': {'total': 0, 'healthy': 0, 'cooling': 0,
                               'disabled': 0, 'in_flight_full': 0},
                },
            })
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [
                {'id': 'glm-5.2', 'object': 'model', 'created': int(time.time())}]})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def write_auth(auth_dir: Path, uid: str, nickname: str) -> None:
    header = base64.urlsafe_b64encode(
        json.dumps({'alg': 'none', 'typ': 'JWT'}).encode()).decode().rstrip('=')
    exp = int(time.time()) + 60 * 86400
    payload = base64.urlsafe_b64encode(
        json.dumps({'iat': int(time.time()), 'exp': exp, 'uid': uid}).encode()).decode().rstrip('=')
    (auth_dir / f'workbuddy-{uid}.json').write_text(json.dumps({
        'account': {'uid': uid, 'enterpriseId': 'ent-demo', 'nickname': nickname},
        'auth': {'accessToken': f'{header}.{payload}.sig', 'refreshToken': 'r',
                 'expiresAt': exp, 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')


def main() -> int:
    check = '--check' in sys.argv
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    auth_dir = DATA / 'auths'
    auth_dir.mkdir(parents=True)
    for a in ACCOUNTS:
        write_auth(auth_dir, a['uid'], a['nickname'])

    billing = ThreadingHTTPServer(('127.0.0.1', BILLING_PORT), Billing)
    threading.Thread(target=billing.serve_forever, daemon=True).start()
    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(auth_dir),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        # 把「直连腾讯」改指到假账单服务：billing_base 在调用时读这个属性
        'WB_CREDIT_BILLING_BASE': f'http://127.0.0.1:{BILLING_PORT}',
        'PYTHONUTF8': '1',
    }
    launcher = (
        'import server.config as c;'
        f"c.TENCENT_BILLING_BASE = {env['WB_CREDIT_BILLING_BASE']!r};"
        'import uvicorn, server.main;'
        f'uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")'
    )
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)

    print(f'假账单: http://127.0.0.1:{BILLING_PORT}')
    print(f'假上游: http://127.0.0.1:{UPSTREAM_PORT}')
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin / {ADMIN_PW})')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        if not check:
            print('服务已起，按 Ctrl+C 退出。')
            proc.wait()
            return 0
        return run_check(env)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        billing.shutdown()
        upstream.shutdown()


def run_check(env: dict) -> int:
    code = r'''
const BASE = process.env.BASE, USER = 'admin', PASS = process.env.PASS;
const OUT = process.env.OUT;
(async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const {pathToFileURL} = await import('node:url');
  const c = path.join(process.env.TEMP || '/tmp', 'wb-i18n-verify', 'node_modules', 'playwright-core', 'index.js');
  const mod = await import(fs.existsSync(c) ? pathToFileURL(c).href : 'playwright-core');
  const chromium = mod.chromium ?? mod.default?.chromium;
  const root = path.join(process.env.LOCALAPPDATA || '', 'ms-playwright');
  const dir = fs.existsSync(root) ? fs.readdirSync(root).filter(d => d.startsWith('chromium-') && !d.includes('headless_shell')).sort().pop() : null;
  const exe = dir ? path.join(root, dir, 'chrome-win64', 'chrome.exe') : undefined;
  const browser = await chromium.launch({executablePath: exe});
  const ctx = await browser.newContext({viewport: {width: 1600, height: 1000}, deviceScaleFactor: 2});
  const page = await ctx.newPage();
  // 统计页面实际起了几个定时器、间隔各是多少：共享时钟是否真的共享，
  // 只有从浏览器侧看才作数（组件源码里写得对，不代表运行时只有一个）。
  await page.addInitScript(() => {
    window.__wbIntervals = [];
    const orig = window.setInterval;
    window.setInterval = function (fn, ms, ...rest) {
      window.__wbIntervals.push(ms);
      return orig.call(this, fn, ms, ...rest);
    };
  });
  const findings = [];
  const step = (ok, label, detail = '') => {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` -- ${detail}` : ''}`);
    if (!ok) findings.push(label + (detail ? ': ' + detail : ''));
  };

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}), page.click('button[type=submit]')]);

  // ── 账号页 ──
  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page.waitForTimeout(3500);
  const body = await page.locator('body').innerText();
  step(/天后到期/.test(body), '账号页出现「N 天后到期」倒计时');
  step(/小时后到期/.test(body), '账号页出现小时级倒计时');
  step(!/已到期/.test(body), '不该有账号显示「已到期」（给出的是未来时间）');

  // 逐个账号核对：每个账号的倒计时档位与额度都要对得上自己的数据，
  // 这是抓「串号」（A 的到期时间显示在 B 上）最直接的方式。
  const rows = page.locator('tbody tr');
  const rowText = async (name) => {
    const n = await rows.count();
    for (let i = 0; i < n; i++) {
      const txt = await rows.nth(i).innerText();
      if (txt.includes(name)) return txt;
    }
    return '';
  };
  // 期望值用区间：倒计时**向下取整**（5 小时整的到期时间，渲染时已过零点几秒 →
  // 4 小时后到期）。取整方向是刻意的保守选择：宁可让用户以为时间更少，也不能多报，
  // 否则用户按「还有 5 小时」安排任务，实际只剩 4 小时多就作废了。
  const expect = [
    ['快到点了', /[12] 分钟后到期/, '400'],
    ['几小时后到期', /[45] 小时后到期/, '800'],
    ['快到期号', /[12] 天后到期/, '1,200'],
    ['还早的号', /4[45] 天后到期/, '4,200'],
    ['多套餐号', /[89] 天后到期/, '1,800'],
  ];
  for (const [name, re, amount] of expect) {
    const txt = await rowText(name);
    step(!!txt, `账号「${name}」在列表中`);
    step(re.test(txt), `「${name}」倒计时档位正确（${re}）`, txt.replace(/\n/g, ' | ').slice(0, 160));
    step(txt.includes(amount), `「${name}」积分显示自己的额度 ${amount}`, txt.replace(/\n/g, ' | ').slice(0, 160));
  }
  const noexp = await rowText('无到期号');
  step(noexp.includes('500') && !/到期/.test(noexp.split('500')[1] || ''),
       '无到期时间的账号不显示倒计时', noexp.replace(/\n/g, ' | ').slice(0, 160));

  // 紧迫度分档按**计算样式**核对，不靠肉眼看截图：7 天是阈值，8 天与 44 天必须
  // 同属「不着急」一档，否则整列都会是暖色，等于没有优先级。
  const tierOf = async (name) => {
    const row = rows.filter({hasText: name}).first();
    const chip = row.locator('span[title*="积分到期时间"]').first();
    if (!await chip.count()) return null;
    return chip.evaluate(el => getComputedStyle(el).color);
  };
  // 三档：1 天内（红）／7 天内（琥珀）／7 天外（常规灰）。三者必须两两不同色 ——
  // 若 7 天外的也是暖色，整列都亮着，等于没有优先级。
  // 时钟按间隔分桶共享：只有「最后一分钟」那个账号该订阅秒级时钟，其余走分钟级。
  // 不分桶的话，几十上百个「30 天后到期」的账号会各自每秒重渲染一次。
  const intervals = await page.evaluate(() => window.__wbIntervals || []);
  const perSec = intervals.filter((ms) => ms === 1000).length;
  const perMin = intervals.filter((ms) => ms === 60000).length;
  step(perSec === 1, `秒级时钟只给最后一分钟的账号（${perSec} 个，期望 1）`,
       `全部间隔=${JSON.stringify(intervals)}`);
  step(perMin >= 1 && perMin <= 3,
       `其余账号共用分钟级时钟（${perMin} 个桶实例，上限 3）`,
       `全部间隔=${JSON.stringify(intervals)}`);

  const tHours = await tierOf('几小时后到期');
  const t2d = await tierOf('快到期号');
  const t8d = await tierOf('多套餐号');
  const t44d = await tierOf('还早的号');
  step(t8d === t44d, `7 天外同色、不与告警档混淆（8 天=${t8d} / 44 天=${t44d}）`);
  step(t2d !== t8d, `7 天内有告警色（2 天=${t2d} ≠ 8 天=${t8d}）`);
  step(t2d !== tHours, `1 天内更醒目（红=${tHours} vs 琥珀=${t2d}）`);
  step(tHours !== t8d, `最急与最缓不同色（${tHours} vs ${t8d}）`);
  await page.screenshot({path: `${OUT}/accounts-light.png`, fullPage: true});

  // 单套餐账号：额度恰好等于总额（4,200），角标应照常带上它
  const farChip = rows.filter({hasText: '还早的号'}).first()
    .locator('span[title*="积分到期时间"]').first();
  step(/^4,200 ·/.test((await farChip.innerText()).trim()),
       '单套餐账号的角标也带额度', await farChip.innerText());
  const farTip = await farChip.getAttribute('title');
  step(/合计 4,200/.test(farTip), '单套餐的悬停合计等于该笔额度', String(farTip).split('\n')[0]);

  // 悬停明细：只校验「多套餐」那行 —— 单套餐的明细没有区分度
  const multiRow = rows.filter({hasText: '多套餐号'}).first();
  const multiChip = multiRow.locator('span[title*="积分到期时间"]').first();
  step(await multiChip.count() > 0, '多套餐账号有倒计时胶囊');
  if (await multiChip.count()) {
    const tip = await multiChip.getAttribute('title');
    const chipText = await multiChip.innerText();
    step(/作废/.test(tip), '悬停说明包含「作废」提示');
    step(/倒计时只显示最近一笔/.test(tip), '悬停说明交代了角标只显示最近一笔');
    step(/1,500/.test(tip) && /300/.test(tip), '悬停明细逐条列出两个套餐的额度', String(tip).replace(/\n/g, ' / '));
    const lines = String(tip).split('\n').filter(l => /\d{4}\//.test(l));
    step(lines.length === 2, `悬停明细行数 = 套餐数（${lines.length}）`);
    step(/合计 1,800/.test(tip), '悬停标题给出合计（可与逐笔对账）', String(tip).split('\n')[0]);
    step(/[89] 天后到期/.test(chipText),
         '胶囊按最早到期的那个套餐算（9 天而非 20 天，1[0-9] 天后即说明取错了）',
         chipText);
    // 关键：角标里的额度必须是**即将到期的那笔**（300），而不是总额（1,800）。
    // 只写时间的话，「1,800 | 8 天后到期」会被读成「1800 全都在 8 天后过期」，
    // 而实际只有 300 是 —— 这是本功能最容易误导人的地方，必须钉死。
    step(/^300 ·/.test(chipText.trim()), '角标额度是即将到期的那笔而非总额', chipText);
    step(!/1,800/.test(chipText), '角标不该出现总额（否则又被读成「全部到期」）', chipText);
  }

  // 倒计时真的在走：等 2 秒读两次，比较「悬停明细里的绝对时刻」不变而秒级重算在跑。
  // 日/小时档 2 秒内不变是正常的，所以这里只验证组件没崩（文本可读且非空）。
  const before = await page.locator('span[title*="积分到期时间"]').first().innerText();
  await page.waitForTimeout(2100);
  const after = await page.locator('span[title*="积分到期时间"]').first().innerText();
  step(!!before && !!after, `倒计时文本稳定可读（${before} -> ${after}）`);

  // ── 首页 ──
  await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
  await page.waitForTimeout(3500);
  const dash = await page.locator('body').innerText();
  step(/最近一笔/.test(dash), '首页积分卡片出现「最近一笔 … 到期」');
  // 卡片必须进告警色：最近一笔 4 小时后到期，属于 7 天内的紧急档，
  // 若还是常规紫色，说明首页没把「快过期」当成需要注意的事。
  const creditCard = page.locator('div.rounded-\\[20px\\]').filter({hasText: '积分余额'}).first();
  const cardColor = await creditCard.evaluate(el => {
    const v = el.querySelector('.tabular-nums');
    return v ? getComputedStyle(v).color : '';
  });
  const warnTone = await page.locator('.text-amber-600.dark\\:text-amber-400').first().evaluate(
    el => getComputedStyle(el).color).catch(() => '');
  step(!!cardColor && cardColor === warnTone,
       `首页积分卡片进入告警色（卡片=${cardColor} / 告警基准=${warnTone}）`);
  await page.screenshot({path: `${OUT}/dashboard-light.png`, fullPage: true});

  // ── 深色模式 ──
  // 用 prefers-color-scheme（主题是 defaultTheme="system"），不要写 localStorage：
  // next-themes 的键名与结构属其内部实现，写死了哪天升级就静默失效 ——
  // 而失效的样子是「深色截图其实是浅色」，看着一切正常。
  // 用 page.emulateMedia 而不是 context.setColorScheme：本机 playwright-core 1.63
  // 的 BrowserContext 上没有 setColorScheme（调用会 undefined 报错），
  // 而 emulateMedia 一定存在。
  await page.emulateMedia({colorScheme: 'dark'});
  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page.waitForTimeout(3000);
  const darkChip = page.locator('span[title*="积分到期时间"]').first();
  const darkBg = await darkChip.evaluate(el => getComputedStyle(el).backgroundColor).catch(() => '');
  const isDark = await page.evaluate(() => document.documentElement.classList.contains('dark'));
  step(isDark, '深色模式确实生效（html.dark）', `html.class=${await page.evaluate(() => document.documentElement.className)}`);
  step(darkBg !== 'rgba(0, 0, 0, 0)' && darkBg !== '', '深色模式下倒计时背景可见', `bg=${darkBg}`);
  // 深色不只是「能看见」：告警色在深色底上仍要与常规色区分开
  const darkTierOf = async (name) => {
    const row = rows.filter({hasText: name}).first();
    const chip = row.locator('span[title*="积分到期时间"]').first();
    return (await chip.count()) ? chip.evaluate(el => getComputedStyle(el).color) : null;
  };
  const dHours = await darkTierOf('几小时后到期');
  const d8d = await darkTierOf('多套餐号');
  step(!!dHours && dHours !== d8d, `深色下告警档仍与常规档区分（${dHours} vs ${d8d}）`);
  await page.screenshot({path: `${OUT}/accounts-dark.png`, fullPage: true});

  await browser.close();
  if (findings.length) { console.log('\nFAILURES:\n' + findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e => { console.error('harness error:', e); process.exit(2); });
'''
    SHOTS.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ['node', '-e', code],
        env={**env, 'BASE': f'http://127.0.0.1:{MANAGER_PORT}', 'PASS': ADMIN_PW,
             'OUT': str(SHOTS)},
        cwd=str(REPO / 'web'), text=True, timeout=300,
    )
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:2000])
    print(f'截图: {SHOTS}')
    return r.returncode


if __name__ == '__main__':
    raise SystemExit(main())
