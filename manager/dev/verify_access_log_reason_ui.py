"""入站日志「拦截原因」的界面验收（开发工具，不参与构建）。

为什么要真起一遍：后端有单测，但这一条改的是**用户能不能看懂日志**。
短码到文案的映射、放行行不显示原因、多语言切换——都只有真渲染才看得见。

    python dev/verify_access_log_reason_ui.py --check

数据落在 dev/.access-log/（已 gitignore），截图输出到 dev/.access-shots/。
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
DATA = REPO / 'dev' / '.access-log'
SHOTS = REPO / 'dev' / '.access-shots'
UPSTREAM_PORT = 7897
MANAGER_PORT = 7898
ADMIN_PW = 'access-pass-123'


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
    """直接在库里造几种拦截记录——比真发请求更可控，且能覆盖全部原因码。"""
    sys.path.insert(0, str(REPO))
    env_db = DATA / 'manager.db'
    from server import config, db
    config.DB_PATH = env_db
    db._conn = None
    db.connect()
    db.add_ip_access_log('10.0.0.9', '/v1/chat/completions', True,
                         'CodexPlusPlus/RelayTest', 'missing_key')
    db.add_ip_access_log('10.0.0.9', '/v1/chat/completions', True,
                         'CodexPlusPlus/RelayTest', 'invalid_key')
    db.add_ip_access_log('10.0.0.7', '/v1/models', True, 'claude-cli/2.1.271',
                         'key_disabled')
    db.add_ip_access_log('10.0.0.7', '/v1/responses', True, 'claude-cli/2.1.271',
                         'realm_mismatch')
    db.add_ip_access_log('10.0.0.5', '/v1/chat/completions', True, 'x', 'ip_blocked')
    db.add_ip_access_log('10.0.0.1', '/v1/models', False, 'ok-client')
    # 未知码：界面应原样显示，不能空白
    db.add_ip_access_log('10.0.0.3', '/v1/chat/completions', True, 'future',
                         'brand_new_code')
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
  const ctx = await browser.newContext({viewport: {width: 1500, height: 1000}, deviceScaleFactor: 2});
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

  await page.goto(`${BASE}/security`, {waitUntil: 'load'});
  await page.waitForTimeout(3000);
  const body = await page.locator('body').innerText();

  // 核心：用户要能看出「为什么被拦」，而不只是「已拦截」
  step(/请求未带 API Key/.test(body), '显示「请求未带 API Key」');
  step(/API Key 无法识别/.test(body), '显示「API Key 无法识别」');
  step(/密钥已停用/.test(body), '显示「密钥已停用」');
  step(/模型版本与密钥限定不符/.test(body), '显示「模型版本与密钥限定不符」');
  step(/来源 IP 被 IP 规则拦截/.test(body), '显示「来源 IP 被 IP 规则拦截」');
  step(/brand_new_code/.test(body), '未知原因码原样显示（不是空白/未知）');

  // 放行行不该带原因
  const rows = page.locator('tbody tr');
  const n = await rows.count();
  let allowedRow = '';
  for (let i = 0; i < n; i++) {
    const txt = await rows.nth(i).innerText();
    if (txt.includes('已放行')) { allowedRow = txt; break; }
  }
  step(!!allowedRow, '存在一行「已放行」');
  step(!!allowedRow && !/未带|无法识别|已停用|不符|规则拦截/.test(allowedRow),
       '放行行不显示任何原因', allowedRow.replace(/\n/g, ' | '));

  // 英文界面下同一批记录要显示英文（短码 + 前端翻译的设计目的）
  await page.evaluate(() => localStorage.setItem('workbuddy-manager:locale', 'en'));
  await page.reload({waitUntil: 'load'});
  await page.waitForTimeout(2500);
  const en = await page.locator('body').innerText();
  step(/request carried no API key/.test(en), '切换英文后原因也跟着变英文');
  step(/API key not recognised/.test(en), '英文下第二条也对');
  step(!/请求未带 API Key/.test(en), '英文界面不该残留中文原因');
  await page.screenshot({path: `${OUT}/security-en.png`, fullPage: true});

  await page.evaluate(() => localStorage.setItem('workbuddy-manager:locale', 'zh-CN'));
  await page.reload({waitUntil: 'load'});
  await page.waitForTimeout(2500);
  await page.screenshot({path: `${OUT}/security-zh.png`, fullPage: true});

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
