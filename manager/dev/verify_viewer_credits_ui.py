"""只读账号的积分口径验收（issue #56）——真服务 + 真浏览器。

现象：同一批账号，管理员看到 2,071 / 6,420 / 9,353 / 3,322；只读账号看到
2,070 / **0** / **0** / 3,323。根因是 `/api/accounts/refresh-credits` 整个
admin-only —— 只读账号的页面加载拿到 403，前端静默吞掉，界面回退到上游
`/status` 的快照值（滞后、可能仍是 0），而 0 还会被标红成「余额耗尽」。

这个脚本起一套「假腾讯账单 + 假上游 + 真管理端」，预置三个积分不同的账号与
两个角色（admin / viewer），然后交给 `dev/verify_viewer_credits.mjs` 用真浏览器
分别登录抓取积分列逐行比对。

    python dev/verify_viewer_credits_ui.py

数据落在 dev/.viewer-credits/（已 gitignore），截图在 dev/.shots-viewer-credits/。
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.viewer-credits'
SHOTS = REPO / 'dev' / '.shots-viewer-credits'
UPSTREAM_PORT = 7931
BILLING_PORT = 7933
MANAGER_PORT = 7932
ADMIN_PW = 'viewer-admin-pass'
VIEWER_PW = 'viewer-readonly-pass'
VIEWER = 'looker'

# 三个账号，积分刻意拉开差距：0 分只在**确实为 0** 时出现（第一个账号）
ACCOUNTS = [
    {'uid': 'vc-empty', 'nickname': '零分号', 'credits': 0},
    {'uid': 'vc-rich', 'nickname': '六千号', 'credits': 6420},
    {'uid': 'vc-mid', 'nickname': '九千号', 'credits': 9353},
    # 账单查询**故意失败**：界面上该账号的积分只能拿到上游快照，
    # 必须被如实标成「上游快照」且不按「余额耗尽」标红（issue #56 的另一半）
    {'uid': 'vc-broken', 'nickname': '故障号', 'credits': 0, 'fail_billing': True},
]


class Billing(BaseHTTPRequestHandler):
    """假腾讯账单域：只认 `get-user-resource`（POST），按 X-User-Id 回余额。

    字段名照真实响应：`CycleCapacityRemain` / `CycleCapacitySize` /
    `CycleCapacityUsed`（`PackageEndTime` 恒为空，到期看 `CycleEndTime`）。
    第一版我按 `RemainAmount` 写、还只实现了 do_GET —— 结果是**所有人的积分都
    查不到**（静默回退到快照），而这正好把断言引到了错的方向：两种角色显示得
    一样（都是 0），看起来「口径一致」。
    """

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

    def do_POST(self):
        n = int(self.headers.get('content-length') or 0)
        if n:
            self.rfile.read(n)
        if 'get-user-resource' not in self.path.split('?')[0]:
            self._json({'code': 0, 'data': {}})
            return
        uid = self.headers.get('X-User-Id') or ''
        acc = next((a for a in ACCOUNTS if a['uid'] == uid), None)
        if acc and acc.get('fail_billing'):
            self._json({'code': 500, 'msg': 'boom'}, status=500)
            return
        credits = acc['credits'] if acc else 0
        self._json({
            'code': 0,
            'data': {
                'Response': {
                    'Data': {
                        'Accounts': [{
                            'PackageName': '套餐',
                            'CycleCapacitySize': credits + 1000,
                            'CycleCapacityRemain': credits,
                            'CycleCapacityUsed': 1000,
                            'PackageEndTime': '',
                            'CycleEndTime': '2026-12-31 23:59:59',
                        }],
                    },
                },
            },
        })

    def do_GET(self):
        self._json({'code': 0, 'data': {}})


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
        if path in ('/healthz', '/status'):
            # 上游快照：**故意给 0**，模拟「上游还没刷新过这些账号」
            # 只读账号若回退到这份数据，就会显示 0（issue #56 的现象）
            self._json({
                'healthy': len(ACCOUNTS), 'total': len(ACCOUNTS),
                'accounts': [
                    {'uid': a['uid'], 'nickname': a['nickname'], 'disabled': False,
                     'cooling': False, 'success_count': 3, 'err_total': 0,
                     'credits': 0}
                    for a in ACCOUNTS
                ],
                'realm_totals': {'cn': {'total': len(ACCOUNTS)},
                                 'global': {'total': 0}},
            })
        elif path == '/v1/models':
            self._json({'object': 'list', 'data': [
                {'id': 'glm-5.2', 'object': 'model', 'created': int(time.time())}]})
        else:
            self._json({'error': 'not found'}, 404)


def write_auth(auth_dir: Path, uid: str, nickname: str) -> None:
    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip('=')

    exp = int(time.time()) + 60 * 86400
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'iat': int(time.time()), 'exp': exp, 'uid': uid})}.sig"
    (auth_dir / f'workbuddy-{uid}.json').write_text(json.dumps({
        'account': {'uid': uid, 'enterpriseId': 'ent', 'nickname': nickname},
        'auth': {'accessToken': token, 'refreshToken': 'r', 'expiresAt': exp,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'},
    }, ensure_ascii=False), encoding='utf-8')


def seed_users(path: Path) -> None:
    """写 users.json：一个管理员 + 一个只读账号。

    用项目自己的 `make_hash`，避免「自造的哈希对不上」导致登录失败。
    """
    sys.path.insert(0, str(REPO))
    from server import security
    path.write_text(json.dumps({
        'secret': base64.urlsafe_b64encode(os.urandom(32)).decode(),
        'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash(ADMIN_PW)},
            {'username': VIEWER, 'role': 'viewer', 'pwd_hash': security.make_hash(VIEWER_PW)},
        ],
    }, ensure_ascii=False), encoding='utf-8')


def _playwright_entry() -> str | None:
    import tempfile
    for base in (Path(tempfile.gettempdir()), Path(os.environ.get('TEMP') or '')):
        p = base / 'wb-i18n-verify' / 'node_modules' / 'playwright-core' / 'index.js'
        if p.is_file():
            return str(p)
    return None


def main() -> int:
    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    auth_dir = DATA / 'auths'
    auth_dir.mkdir(parents=True)
    for a in ACCOUNTS:
        write_auth(auth_dir, a['uid'], a['nickname'])
    seed_users(DATA / 'users.json')

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
        # 把「直连腾讯查积分」指到假账单服务
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
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin/{ADMIN_PW}、{VIEWER}/{VIEWER_PW})')
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(
                    f'http://127.0.0.1:{MANAGER_PORT}/api/healthz', timeout=1)
                break
            except Exception:
                time.sleep(0.5)

        node_env = {
            **os.environ,
            'WB_BASE': f'http://127.0.0.1:{MANAGER_PORT}',
            'WB_ADMIN': 'admin',
            'WB_ADMIN_PW': ADMIN_PW,
            'WB_VIEWER': VIEWER,
            'WB_VIEWER_PW': VIEWER_PW,
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_viewer_credits.mjs'], cwd=str(REPO),
                           env=node_env, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        print(r.stdout or '')
        if r.stderr:
            print(r.stderr[-2000:], file=sys.stderr)
        if r.returncode != 0:
            return r.returncode
        if 'ALL CHECKS PASSED' not in (r.stdout or ''):
            print('✗ 浏览器侧没有报 ALL CHECKS PASSED —— 不能当作通过', file=sys.stderr)
            return 1
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        billing.shutdown()
        upstream.shutdown()


if __name__ == '__main__':
    sys.exit(main())
