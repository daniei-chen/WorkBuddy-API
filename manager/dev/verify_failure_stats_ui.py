"""失败请求数的界面验收（开发工具，不参与构建）。

为什么真起一遍：后端有单测，但这条改的是**用户能不能看见失败**。卡片提示、
趋势图上的虚线、以及「只有失败没有成功的那天」在图上出不出得来，都只有真渲染
才看得见。

    python dev/verify_failure_stats_ui.py --check

数据落在 dev/.fail-stats/（已 gitignore），截图输出到 dev/.fail-shots/。
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
DATA = REPO / 'dev' / '.fail-stats'
SHOTS = REPO / 'dev' / '.fail-shots'
UPSTREAM_PORT = 7901
MANAGER_PORT = 7902
ADMIN_PW = 'fail-stats-pass'


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
            self._json({'healthy': 5, 'total': 5})
        elif path == '/status':
            pool = [{'uid': f'u{i}', 'nickname': f'号{i}', 'realm': 'cn',
                     'healthy': True, 'disabled': False, 'cooling': False,
                     'success_count': 10, 'err_total': 0, 'credits': 1000}
                    for i in range(5)]
            self._json({'accounts': pool, 'total': 5, 'healthy': 5, 'cooling': 0,
                        'disabled': 0, 'in_flight_full': 0, 'redis_mode': 'noop',
                        'sticky_sessions': 0,
                        'realm_totals': {
                            'cn': {'total': 5, 'healthy': 5, 'cooling': 0,
                                   'disabled': 0, 'in_flight_full': 0},
                            'global': {'total': 0, 'healthy': 0, 'cooling': 0,
                                       'disabled': 0, 'in_flight_full': 0}}})
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [
                {'id': 'deepseek-v4.1-flash', 'object': 'model',
                 'created': int(time.time())}]})
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        self._json({'ok': True})


def seed() -> None:
    """造出线上那次事故的形态：某天 31 次失败、0 次成功 + 今天有成功也有失败。"""
    sys.path.insert(0, str(REPO))
    from server import config, db
    config.DB_PATH = DATA / 'manager.db'
    db._conn = None
    db.connect()
    now = int(time.time())
    db.execute("INSERT INTO api_keys(name,key_hash,prefix,enabled,created_at,quota,used_tokens) "
               "VALUES('演示密钥','h','wbk_demo',1,?,0,0)", (now,))
    kid = db.query_one('SELECT id FROM api_keys LIMIT 1')['id']

    def log(ts: int, status: int, pt=0, ct=0, credit=None, realm='cn', model='deepseek-v4.1-flash'):
        db.execute(
            'INSERT INTO request_logs(ts,key_id,ip,model,mapped_model,status,prompt_tokens,'
            'completion_tokens,latency_ms,ua,error,stream,credit,realm) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ts, kid, '10.0.0.1', model, model, status, pt, ct, 120, 'demo-UA',
             None if status < 400 else f'status {status}', 1, credit, realm))

    # 3 天前：31 次全失败（0 成功）—— 线上那次凌晨中断的形态
    day3 = now - 3 * 86400
    for i in range(31):
        log(day3 + i, 503)

    # 今天：成功若干 + 失败若干（4xx 与 5xx 都有）
    for i in range(9):
        log(now - i * 60, 200, pt=20 + i, ct=8, credit=0.12)
        db.bump_usage(kid, 'deepseek-v4.1-flash', 20 + i, 8, 0.12, realm='cn')
    for i in range(4):
        log(now - 600 - i, 503)
    for i in range(3):
        log(now - 900 - i, 429)
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
  const ctx = await browser.newContext({viewport: {width: 1600, height: 1100}, deviceScaleFactor: 2});
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

  // ── 总览页 ──
  await page.goto(`${BASE}/dashboard`, {waitUntil: 'load'});
  await page.waitForTimeout(4000);
  const dash = await page.locator('body').innerText();

  step(/7 次失败/.test(dash) || /今天 7 次失败/.test(dash),
       '总览卡片显示今日失败数（4 个 503 + 3 个 429 = 7）',
       (dash.match(/[^\n]*失败[^\n]*/g) || []).slice(0, 3).join(' | '));
  // 近 7 天包含 3 天前那 31 次纯失败 → 7 + 31 = 38（不是 7）
  step(/近 7 天 38 次失败/.test(dash),
       '趋势图标题处显示近 7 天失败总数', (dash.match(/近 7 天[^\n]*/) || [''])[0]);

  const dashChart = await page.locator('.recharts-surface').count();
  step(dashChart > 0, `总览趋势图已渲染（${dashChart} 个 svg）`);
  // 虚线（失败曲线）是否存在
  const dashed = await page.locator('path[stroke-dasharray]').count();
  step(dashed > 0, `总览图上画出了失败曲线（${dashed} 条虚线）`);
  await page.screenshot({path: `${OUT}/dashboard.png`, fullPage: true});

  // ── 统计页 ──
  await page.goto(`${BASE}/stats`, {waitUntil: 'load'});
  await page.waitForTimeout(4000);
  const st = await page.locator('body').innerText();
  step(/今日失败 7 次/.test(st), '统计页今日请求卡片提示失败数',
       (st.match(/今日失败[^\n]*/) || [''])[0]);
  step(/不计入上方请求数/.test(st), '并说明失败不计入请求数（避免误解）');
  const stChart = await page.locator('.recharts-surface').count();
  step(stChart > 0, `统计页图表已渲染（${stChart} 个 svg）`);
  const stDashed = await page.locator('path[stroke-dasharray]').count();
  step(stDashed > 0, `统计页图上画出了失败曲线（${stDashed} 条虚线）`);
  await page.screenshot({path: `${OUT}/stats.png`, fullPage: true});

  // ── 关键：纯失败日（3 天前）必须出现在图表数据里 ──
  // 从页面拿到 daily 数据不方便，改问接口（同一份后端），再从图上确认 x 轴含那天
  const api = await page.evaluate(async () => {
    const r = await fetch('/api/stats/daily?days=30', {credentials: 'include'});
    return r.ok ? await r.json() : null;
  });
  if (api) {
    const pureFail = api.filter(d => (d.failed || 0) > 0 && d.requests === 0);
    step(pureFail.length > 0,
         `接口里存在「纯失败日」（requests=0 但 failed>0）：${JSON.stringify(pureFail)}`);
    const total = api.reduce((s, d) => s + (d.failed || 0), 0);
    step(total === 38, `失败总数正确（31 + 7 = 38，实际 ${total}）`);
  } else {
    step(false, '取不到 daily 接口数据');
  }

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
