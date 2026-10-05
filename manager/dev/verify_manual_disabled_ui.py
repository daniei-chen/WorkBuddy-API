"""账号页「上游状态位停用」的界面验收（issue #45）。

上游 2026-09-19 暴露了 `manual_disabled` 状态位，语义是「只摘对话流量」——
账号仍在池里、**签到与保活照常**。面板要能：

  1. 把这种账号显示成「已停用」，而**不是**「在线」；
  2. 与改名停用（`disabled_by_panel`）**区分开**——两者的代价完全不同
     （任务停不停），文案必须说清，否则用户以为积分还在涨；
  3. 停用期间**仍然显示**签到 / 测试 / 刷新按钮——「任务照常」正是这条路的
     意义，藏掉按钮就等于把它与改名那条的区别抹掉了。

真实数据来自假上游的 /status，故这里起一个假上游 + 真 uvicorn。

    python dev/verify_manual_disabled_ui.py

数据落在 dev/.manual-disabled/（已 gitignore），截图输出到 dev/.shots-45/。
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
DATA = REPO / 'dev' / '.manual-disabled'
SHOTS = REPO / 'dev' / '.shots-45'
UPSTREAM_PORT = 7903
MANAGER_PORT = 7904
ADMIN_PW = 'manual-disabled-pass'

UID_BIT = 'aaaa1111-0000-0000-0000-000000000001'    # 状态位停用
UID_RENAMED = 'bbbb2222-0000-0000-0000-000000000002'  # 改名停用
UID_NORMAL = 'cccc3333-0000-0000-0000-000000000003'   # 正常在线


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
            self._json({'healthy': 2, 'total': 3})
        elif path == '/status':
            # 状态位停用的账号**仍在池里**（这正是与改名那条的本质区别），
            # 所以它出现在 accounts 里、且带 manual_disabled=true。
            # 改名停用的账号不在池里（上游不加载它）。
            self._json({
                'accounts': [
                    {'uid': UID_BIT, 'nickname': '状态位停用号',
                     'credits': 1200, 'manual_disabled': True,
                     'manual_reason': '在拖后腿，先摘一会', 'disabled': False,
                     'cooling': False, 'success_count': 12, 'in_flight': 0},
                    {'uid': UID_NORMAL, 'nickname': '正常号',
                     'credits': 800, 'manual_disabled': False, 'disabled': False,
                     'cooling': False, 'success_count': 30, 'in_flight': 0},
                ],
                'total': 2, 'healthy': 1, 'cooling': 0, 'disabled': 0,
                'in_flight_full': 0, 'redis_mode': 'noop', 'sticky_sessions': 0,
                'realm_totals': {
                    'cn': {'total': 2, 'healthy': 1, 'cooling': 0,
                           'disabled': 0, 'in_flight_full': 0},
                    'global': {'total': 0, 'healthy': 0, 'cooling': 0,
                               'disabled': 0, 'in_flight_full': 0}},
            })
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [{'id': 'glm-5.2'}]})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        # /admin/accounts/{uid}/disable|enable
        if '/admin/accounts/' in self.path:
            self._json({'uid': self.path.split('/')[3], 'manual_disabled': True,
                        'changed': True})
        else:
            self._json({'ok': True})


def _jwt(uid: str) -> str:
    import base64
    h = base64.urlsafe_b64encode(
        json.dumps({'alg': 'none', 'typ': 'JWT'}).encode()).decode().rstrip('=')
    p = base64.urlsafe_b64encode(json.dumps({
        'iat': int(time.time()), 'exp': int(time.time()) + 60 * 86400,
        'uid': uid}).encode()).decode().rstrip('=')
    return f'{h}.{p}.sig'


def seed() -> None:
    # dev/ 不在包路径上（脚本是直接 `python dev/xxx.py` 跑的），要显式加仓库根
    sys.path.insert(0, str(REPO))
    from server import config, db
    config.DB_PATH = DATA / 'manager.db'
    db._conn = None
    db.connect()
    auths = DATA / 'auths'
    auths.mkdir(parents=True, exist_ok=True)

    for uid, name, disabled in ((UID_BIT, '状态位停用号', False),
                                (UID_RENAMED, '改名停用号', True),
                                (UID_NORMAL, '正常号', False)):
        fname = f'workbuddy-{uid}.json' + ('.disabled' if disabled else '')
        (auths / fname).write_text(json.dumps({
            'account': {'uid': uid, 'enterpriseId': 'e', 'nickname': name},
            'auth': {'accessToken': _jwt(uid), 'refreshToken': 'r',
                     'expiresAt': int(time.time()) + 60 * 86400,
                     'domain': 'copilot.tencent.com', 'realm': 'cn'},
        }, ensure_ascii=False), encoding='utf-8')
    db._conn.close()
    db._conn = None


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)

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

    launcher = ('import uvicorn, server.main;'
                f'uvicorn.run(server.main.app, host="127.0.0.1", '
                f'port={MANAGER_PORT}, log_level="warning")')
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

  await page.goto(`${BASE}/accounts`, {waitUntil: 'load'});
  await page.waitForTimeout(4000);
  const body = await page.locator('body').innerText();

  // 1. 状态位停用的账号必须显示成「已停用」，不能是「在线」
  const rowBit = page.locator('tr', {hasText: '状态位停用号'}).first();
  const bitText = (await rowBit.count()) ? await rowBit.innerText() : '';
  step(/已停用/.test(bitText), '状态位停用的账号显示为「已停用」', bitText.replace(/\n/g, ' | ').slice(0, 90));
  step(!/● 在线/.test(bitText), '状态位停用的账号没有被误报成「在线」');

  // 2. 正常账号仍是「在线」（确认没把整列改坏）
  const rowOk = page.locator('tr', {hasText: '正常号'}).first();
  const okText = (await rowOk.count()) ? await rowOk.innerText() : '';
  step(/在线/.test(okText), '正常账号仍显示「在线」');

  // 3. 改名停用的账号也要显示（它是文件级的，不能因为不在池里就消失）
  const rowRe = page.locator('tr', {hasText: '改名停用号'}).first();
  step(await rowRe.count() > 0, '改名停用的账号仍在列表里（可再启用）');

  // 4. 悬停提示要说清「任务照常」——这是两条路的关键区别
  const badge = rowBit.locator('span', {hasText: '已停用'}).first();
  let tip = '';
  if (await badge.count()) {
    tip = await badge.getAttribute('title') || '';
  }
  step(/签到|保活/.test(tip), '提示里说明了「签到与保活照常」', tip.slice(0, 70));

  // 5. 停用期间签到 / 测试 / 刷新按钮**仍然可见**（这条路的意义所在）
  const btns = await rowBit.locator('button').count();
  step(btns >= 3, '状态位停用期间仍显示操作按钮（任务照常）', `该行按钮数=${btns}`);

  await page.screenshot({path: `${OUT}/accounts-manual-disabled.png`, fullPage: true});
  await browser.close();
  if (findings.length) { console.log('\nFAILURES:\n' + findings.join('\n')); process.exit(1); }
  console.log('\nALL CHECKS PASSED');
})().catch(e => { console.error('harness error:', e); process.exit(2); });
'''


if __name__ == '__main__':
    raise SystemExit(main())
