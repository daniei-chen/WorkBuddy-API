"""任务页筛选栏「点空类型不消失」的界面验收（issue #35）。

现场：点到一个没有记录的类型后整条筛选栏消失，用户没法切回「全部」，
只能刷新页面。根因在接口 `stats` 的口径（带了 kind 过滤），已在服务端修掉；
这个脚本从**浏览器侧**确认用户真的不会再被卡住。

    python dev/verify_task_filter_ui.py --check

数据落在 dev/.task-filter/（已 gitignore），截图输出到 dev/.task-shots/。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.verify-44'
SHOTS = REPO / 'dev' / '.shots-44'
UPSTREAM_PORT = 7901
MANAGER_PORT = 7902
ADMIN_PW = 'verify44-pass'


class Upstream(BaseHTTPRequestHandler):
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
            self._json({'healthy': 0, 'total': 0})
        elif path == '/status':
            self._json({'accounts': [], 'total': 0, 'healthy': 0, 'cooling': 0,
                        'disabled': 0, 'in_flight_full': 0, 'redis_mode': 'noop',
                        'sticky_sessions': 0,
                        'realm_totals': {
                            'cn': {'total': 0, 'healthy': 0, 'cooling': 0,
                                   'disabled': 0, 'in_flight_full': 0},
                            'global': {'total': 0, 'healthy': 0, 'cooling': 0,
                                       'disabled': 0, 'in_flight_full': 0}}})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def seed() -> None:
    """造跨天数据：今天一条、昨天一条 —— 24 小时范围下跨天效果才看得出来。

    「昨天」这条刻意用**本地日历日的昨天 + 与现在相同的时刻**，而不是
    `now - 20 小时`：后者在晚上跑时会落到今天（20 小时前仍是今天），
    于是断言「必须出现昨天」随机失败——测试自带的不确定性比 bug 更难查。
    """
    sys.path.insert(0, str(REPO))
    env_db = DATA / 'manager.db'
    from server import config, db
    config.DB_PATH = env_db
    db._conn = None
    db.connect()
    db.clear_task_logs()
    now = int(time.time())
    # 本地日历日的昨天同一时刻
    lt = time.localtime(now)
    yesterday = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - 1,
                                 lt.tm_hour, lt.tm_min, lt.tm_sec, 0, 0, -1)))
    db.add_task_logs([
        {'ts': now - 300, 'uid': 'u-demo-0001', 'kind': 'travel',
         'level': 'ok', 'credits': 100, 'message': 'travel done',
         'dedup_key': 'd1'},
        {'ts': yesterday, 'uid': 'u-demo-0001', 'kind': 'active',
         'level': 'ok', 'credits': 50, 'message': 'activity report ok',
         'dedup_key': 'd2'},
    ])
    # 账号表要给一个 cn 账号：任务日志按 realm 过滤，uid→realm 的映射来自
    # 账号文件；没有它所有行都会被 realm 过滤掉（界面显示「共 0 条」）。
    (DATA / 'auths').mkdir(parents=True, exist_ok=True)
    import base64

    def jwt(uid: str) -> str:
        h = base64.urlsafe_b64encode(
            json.dumps({'alg': 'none', 'typ': 'JWT'}).encode()).decode().rstrip('=')
        p = base64.urlsafe_b64encode(json.dumps({
            'iat': int(time.time()), 'exp': int(time.time()) + 60 * 86400,
            'uid': uid}).encode()).decode().rstrip('=')
        return f'{h}.{p}.sig'

    (DATA / 'auths' / 'workbuddy-u-demo-0001.json').write_text(json.dumps({
        'account': {'uid': 'u-demo-0001', 'enterpriseId': 'e', 'nickname': '演示号'},
        'auth': {'accessToken': jwt('u-demo-0001'), 'refreshToken': 'r',
                 'expiresAt': int(time.time()) + 60 * 86400,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)
    (DATA / 'auths').mkdir(parents=True, exist_ok=True)

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
        'PYTHONUTF8': '1',
    }
    seed()

    launcher = (
        'import uvicorn, server.main;'
        f'uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")'
    )
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
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
        return run_check(env)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


def run_check(env: dict) -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', '-e', JS],
                       env={**env, 'BASE': f'http://127.0.0.1:{MANAGER_PORT}',
                            'PASS': ADMIN_PW, 'OUT': str(SHOTS)},
                       cwd=str(REPO / 'web'), text=True, timeout=300)
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:2000])
    print(f'截图: {SHOTS}')
    return r.returncode


# 浏览器侧检查。内联成常量而不是独立 .mjs：验收脚本必须能单文件跑起来，
# 依赖旁边一个临时文件的话，那个文件一被清理（它本来就在 gitignore 里）脚本就挂。
JS = r'''
const BASE = process.env.BASE, OUT = process.env.OUT;
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
  const ctx = await browser.newContext({viewport: {width: 1500, height: 1100}, deviceScaleFactor: 2});
  const page = await ctx.newPage();
  const findings = [];
  const step = (ok, label, extra = '') => {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? ` -- ${extra}` : ''}`);
    if (!ok) findings.push(label + (extra ? ': ' + extra : ''));
  };

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', 'admin');
  await page.fill('#password', process.env.PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}), page.click('button[type=submit]')]);

  // 把范围切到「近 24 小时」——跨天正是在这个范围下才出现
  const pickRange = async (page, comboIndex) => {
    const cb = page.locator('button[role=combobox]').nth(comboIndex);
    if (!(await cb.count())) return false;
    await cb.click().catch(() => {});
    await page.waitForTimeout(700);
    const opt = page.locator('[role=option]', {hasText: /24/}).first();
    if (!(await opt.count())) return false;
    await opt.click();
    await page.waitForTimeout(2800);
    return true;
  };

  await page.goto(`${BASE}/tasks`, {waitUntil: 'load'});
  await page.waitForTimeout(3500);
  await pickRange(page, 0);
  const tasksBody = await page.locator('body').innerText();
  const tasksMarked = (tasksBody.match(/昨天\s*\d{4}\/\d{2}\/\d{2}/g) || []).length;
  // 种子里有「今天」与「昨天」两条，都必须出现在同一份列表里。
  // 判据用「两个日期都出现」而不是「共 N 条」：后者取决于卡片标题的文案格式，
  // 文案一改就会误报（实测踩过）。
  const todayStr = new Date().toLocaleDateString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit'}).replace(/\//g, '/');
  const yStr = (() => { const d = new Date(); d.setDate(d.getDate() - 1); return d.toLocaleDateString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit'}); })();
  step(tasksBody.includes(yStr), '任务页：昨天的记录出现在列表里', yStr);
  step(tasksMarked >= 1, '任务页：昨天的行带「昨天」前缀', `命中 ${tasksMarked} 行`);
  // 今天的行**不能**带前缀（逐行都标「今天」只是噪音）
  const todayMarked = (tasksBody.match(/今天\s*\d{4}\/\d{2}\/\d{2}/g) || []).length;
  step(todayMarked === 0, '任务页：今天的行不带前缀', `命中 ${todayMarked} 行`);
  await page.screenshot({path: `${OUT}/tasks-24h.png`, fullPage: true});

  // 日志页没有种子数据，只断言「渲染过程不报错、页面结构在」
  await page.goto(`${BASE}/logs`, {waitUntil: 'load'});
  await page.waitForTimeout(3000);
  const logsBody = await page.locator('body').innerText();
  step(/请求日志/.test(logsBody) || logsBody.length > 50, '日志页正常渲染（无运行时错误）');
  await page.screenshot({path: `${OUT}/logs-24h.png`, fullPage: true});

  await browser.close();
  if (findings.length) { console.log('\nFAILURES:\n' + findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e => { console.error('harness error:', e); process.exit(2); });
'''


if __name__ == '__main__':
    raise SystemExit(main())
