"""端到端验收：开启「账号管理接口」后，「临时停用」真的走状态位（issue #45）。

用户反馈的完整场景是：

  1. 升级到新版，点「临时停用」→ 看到「已改用改名方式」的回退提示；
  2. （本脚本新增）到设置页打开「账号管理接口」开关；
  3. 重新停用 → **应该**走状态位：不改文件名、账号仍在池里、签到与保活照常。

第 3 步是本脚本要证明的——前面那些测试各自只覆盖一段（配置能存、路由能调），
没有任何一条走通「用户按提示操作完 → 行为真的变了」。

假上游在这里是**有状态**的：`admin.enabled` 打开后才注册那三个管理路由，
以此模拟「配置生效需要重启上游」的真实行为。

    python dev/verify_admin_flow_e2e.py

数据落在 dev/.admin-flow/（已 gitignore）。
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
DATA = REPO / 'dev' / '.admin-flow'
UPSTREAM_PORT = 7907
MANAGER_PORT = 7908
ADMIN_PW = 'admin-flow-pass'
UID = 'eeee5555-0000-0000-0000-000000000005'

# 假上游的运行时状态：admin 开关 + 被状态位停用的 uid 集合
_STATE = {'admin_enabled': False, 'manual_disabled': set()}
_STATE_LOCK = threading.Lock()


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

    def _text(self, text, status=404):
        body = text.encode()
        self.send_response(status)
        self.send_header('content-type', 'text/plain; charset=utf-8')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/healthz':
            self._json({'healthy': 1, 'total': 1})
        elif path == '/status':
            with _STATE_LOCK:
                stopped = UID in _STATE['manual_disabled']
            self._json({
                'accounts': [{'uid': UID, 'nickname': '演示号', 'credits': 900,
                              'manual_disabled': stopped, 'disabled': False,
                              'cooling': False, 'success_count': 5, 'in_flight': 0}],
                'total': 1, 'healthy': 1, 'cooling': 0, 'disabled': 0,
                'in_flight_full': 0, 'redis_mode': 'noop', 'sticky_sessions': 0,
                'realm_totals': {
                    'cn': {'total': 1, 'healthy': 1, 'cooling': 0,
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
        if '/admin/accounts/' in self.path:
            with _STATE_LOCK:
                enabled = _STATE['admin_enabled']
            if not enabled:
                # 未开启：上游**根本没注册**这些路由 → net/http 默认纯文本 404
                self._text('404 page not found\n')
                return
            action = self.path.rstrip('/').split('/')[-1]
            uid = self.path.split('/')[3]
            with _STATE_LOCK:
                if action == 'disable':
                    _STATE['manual_disabled'].add(uid)
                elif action == 'enable':
                    _STATE['manual_disabled'].discard(uid)
            self._json({'uid': uid, 'manual_disabled': action == 'disable',
                        'changed': True})
            return
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
    sys.path.insert(0, str(REPO))
    from server import config, db
    config.DB_PATH = DATA / 'manager.db'
    db._conn = None
    db.connect()
    auths = DATA / 'auths'
    auths.mkdir(parents=True, exist_ok=True)
    (auths / f'workbuddy-{UID}.json').write_text(json.dumps({
        'account': {'uid': UID, 'enterpriseId': 'e', 'nickname': '演示号'},
        'auth': {'accessToken': _jwt(UID), 'refreshToken': 'r',
                 'expiresAt': int(time.time()) + 60 * 86400,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')
    db._conn.close()
    db._conn = None
    (DATA / 'config.json').write_text(json.dumps({
        'listen': '127.0.0.1:7863', 'api_key': 'upstream-key-for-test',
        'admin': {'enabled': False},
    }, ensure_ascii=False, indent=2), encoding='utf-8')


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    DATA.mkdir(parents=True)

    upstream = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB2API_KEY': 'upstream-key-for-test',
        'WB_AUTH_DIR': str(DATA / 'auths'),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_UPSTREAM_CONFIG': str(DATA / 'config.json'),
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
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        return run_checks()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()


def run_checks() -> int:
    import httpx

    findings: list[str] = []

    def step(ok: bool, label: str, extra: str = '') -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f' -- {extra}' if extra else ''))
        if not ok:
            findings.append(label + (f': {extra}' if extra else ''))

    client = httpx.Client(base_url=f'http://127.0.0.1:{MANAGER_PORT}',
                          timeout=30, trust_env=False)
    r = client.post('/api/login', json={'username': 'admin', 'password': ADMIN_PW})
    assert r.status_code == 200, r.text[:200]
    cookies = r.cookies

    def accounts() -> list[dict]:
        rr = client.get('/api/accounts', cookies=cookies)
        return (rr.json() or {}).get('accounts') or []

    def auth_file_exists(disabled: bool) -> bool:
        name = f'workbuddy-{UID}.json' + ('.disabled' if disabled else '')
        return (DATA / 'auths' / name).is_file()

    # ── 1. 开关关闭时：停用走改名回退 ──
    before = accounts()
    step(len(before) == 1, '账号列表有 1 个账号')
    r = client.post(f'/api/accounts/workbuddy-{UID}.json/disabled',
                    json={'disabled': True}, cookies=cookies)
    body = r.json()
    step(r.status_code == 200, '停用请求成功')
    step(body.get('via') == 'rename', '开关关闭时走改名路径', f"via={body.get('via')}")
    step(body.get('bit_code') == 'no_route', '识别为「上游未开放管理接口」')
    step(auth_file_exists(disabled=True), '文件名被改名（回退路径的代价）')
    step('设置' in body.get('message', ''),
         '提示里指出了开启位置', body.get('message', '')[:60])

    # 启回来，回到干净状态
    client.post(f'/api/accounts/workbuddy-{UID}.json.disabled/disabled',
                json={'disabled': False}, cookies=cookies)
    step(auth_file_exists(disabled=False), '启用后文件名还原')

    # ── 2. 打开开关（等价于上游重启后加载了新配置）──
    r = client.get('/api/settings/upstream', cookies=cookies)
    step((r.json() or {}).get('admin', {}).get('enabled') is False,
         '前置：面板读到的 admin.enabled 为 false')
    r = client.post('/api/settings/upstream', json={'admin': {'enabled': True}},
                    cookies=cookies)
    step(r.status_code == 200, '保存 admin.enabled=true 成功', r.text[:120])
    saved = json.loads((DATA / 'config.json').read_text(encoding='utf-8'))
    step(saved.get('admin', {}).get('enabled') is True, '配置已落到上游 config.json')

    # 模拟「配置生效」：上游本该重启（我们无法在这里重启一个假上游，
    # 直接翻转它的运行时开关，语义等价）
    with _STATE_LOCK:
        _STATE['admin_enabled'] = True
    step(True, '模拟上游重启后加载新配置（内存态开关翻转）')

    # ── 3. 再停用一次：这次必须走状态位，且**不改名** ──
    r = client.post(f'/api/accounts/workbuddy-{UID}.json/disabled',
                    json={'disabled': True}, cookies=cookies)
    body = r.json()
    step(body.get('via') == 'manual_disabled',
         '开启后走状态位（不再改名）', f"via={body.get('via')}")
    step(auth_file_exists(disabled=False),
         '**文件名没被改动**（账号仍在账号池里）')
    step('签到' in body.get('message', '') or '保活' in body.get('message', ''),
         '提示说明签到与保活照常', body.get('message', '')[:60])

    # ── 4. 面板如实反映停用状态（读上游 /status）──
    rows = accounts()
    step(len(rows) == 1, '停用后账号仍在列表里')
    step(rows[0].get('manual_disabled') is True,
         '列表里标出「状态位停用」', f"manual_disabled={rows[0].get('manual_disabled')}")

    # ── 5. 启用：清掉状态位 ──
    r = client.post(f'/api/accounts/workbuddy-{UID}.json/disabled',
                    json={'disabled': False}, cookies=cookies)
    step(r.json().get('via') == 'manual_disabled', '启用同样走状态位')
    rows = accounts()
    step(rows[0].get('manual_disabled') is False, '启用后状态位已清除')

    client.close()
    if findings:
        print('\nFAILURES:\n' + '\n'.join(findings))
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
