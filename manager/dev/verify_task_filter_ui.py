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
DATA = REPO / 'dev' / '.task-filter'
SHOTS = REPO / 'dev' / '.task-shots'
UPSTREAM_PORT = 7899
MANAGER_PORT = 7900
ADMIN_PW = 'task-filter-pass'


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
    """造两类有记录、一类没有（credit 永远是空的——正是用户点进去的那种）。"""
    sys.path.insert(0, str(REPO))
    env_db = DATA / 'manager.db'
    from server import config, db
    config.DB_PATH = env_db
    db._conn = None
    db.connect()
    db.clear_task_logs()
    db.add_task_logs([
        {'ts': int(time.time()) - 300, 'uid': 'u-demo-0001', 'kind': 'travel',
         'level': 'ok', 'credits': 100, 'message': 'travel done location=1',
         'dedup_key': 'd1'},
        {'ts': int(time.time()) - 200, 'uid': 'u-demo-0001', 'kind': 'active',
         'level': 'ok', 'credits': 50, 'message': 'activity report ok',
         'dedup_key': 'd2'},
        {'ts': int(time.time()) - 100, 'uid': 'u-demo-0001', 'kind': 'keepalive',
         'level': 'ok', 'credits': 0, 'message': 'keepalive ok',
         'dedup_key': 'd3'},
    ])
    # 账号表要给一个 cn 账号，否则 realm 过滤会把所有行都滤掉
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
    db._conn.close()
    db._conn = None


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
    code = r'''
const BASE = process.env.BASE, USER = 'admin', PASS = process.env.PASS, OUT = process.env.OUT;
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
  const step = (ok, label, detail = '') => {
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` -- ${detail}` : ''}`);
    if (!ok) findings.push(label + (detail ? ': ' + detail : ''));
  };

  await page.goto(`${BASE}/login`, {waitUntil: 'domcontentloaded'});
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  await Promise.all([page.waitForURL(/dashboard/).catch(() => {}), page.click('button[type=submit]')]);

  await page.goto(`${BASE}/tasks`, {waitUntil: 'load'});
  await page.waitForTimeout(4000);

  // 筛选栏的「全部」按钮是识别它的锚点
  const allBtn = page.locator('button', {hasText: /^全部/}).first();
  step(await allBtn.count() > 0, '筛选栏存在（「全部」按钮可见）');

  // 找一个**明确为空**的类型按钮（计数器显示 0，或标题带「没有记录」）
  const chips = page.locator('button[title*="没有记录"]');
  const emptyCount = await chips.count();
  step(emptyCount > 0, '存在「当前范围内没有记录」的类型按钮', `找到 ${emptyCount} 个`);

  if (emptyCount > 0) {
    const target = chips.first();
    const name = (await target.innerText()).trim();
    await target.click();
    await page.waitForTimeout(2500);

    // 核心：点完之后筛选栏**必须还在**
    const stillThere = await page.locator('button', {hasText: /^全部/}).count();
    step(stillThere > 0, `点空类型（${name}）后筛选栏仍在`, `「全部」按钮数=${stillThere}`);
    const body = await page.locator('body').innerText();
    step(/没有记录/.test(body), '显示了「该类型没有记录」的提示（而不是空白）');

    // 还能点回「全部」
    const back = page.locator('button', {hasText: /^全部/}).first();
    if (await back.count()) {
      await back.click();
      await page.waitForTimeout(2500);
      const rows = await page.locator('tbody tr').count();
      step(rows > 0, `能切回「全部」并看到记录（${rows} 行）`);
    } else {
      step(false, '筛选栏消失，无法切回「全部」');
    }
  }

  await page.screenshot({path: `${OUT}/tasks-filter.png`, fullPage: true});
  await browser.close();
  if (findings.length) { console.log('\nFAILURES:\n' + findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e => { console.error('harness error:', e); process.exit(2); });
'''
    SHOTS.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(['node', '-e', code],
                       env={**env, 'BASE': f'http://127.0.0.1:{MANAGER_PORT}',
                            'PASS': ADMIN_PW, 'OUT': str(SHOTS)},
                       cwd=str(REPO / 'web'), text=True, timeout=300)
    print(r.stdout or '')
    if r.stderr:
        print(r.stderr[:2000])
    print(f'截图: {SHOTS}')
    return r.returncode


if __name__ == '__main__':
    raise SystemExit(main())
