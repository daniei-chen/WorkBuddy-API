"""复现「首页说在线、账号列表说未加载」的最小环境。

起一个假上游（账号池里**故意少一个**账号）+ 管理端，并在 auths/ 下放两个
账号文件——其中一个上游不会加载。这正是用户截图里的场景。

    python dev/repro_pool_mismatch.py

起好后访问 http://127.0.0.1:7864（admin / repro-pass-123），对照
首页「账号健康快照」与「账号」页的状态列是否说法一致。
Ctrl+C 退出；数据落在 dev/.repro/（已 gitignore）。
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
DATA = REPO / 'dev' / '.repro'
UPSTREAM_PORT = 7863
MANAGER_PORT = 7864
ADMIN_PW = 'repro-pass-123'

# 由 main() 依据命令行开关设置：true = 摆出所有状态档
STATUS_MODE = False

# 用户截图里的两个账号
IN_POOL = {
    'uid': 'e366892d-88eb-4023-a990-4e776d40ac40', 'nickname': 'Htibinak',
    'realm': 'cn', 'healthy': True, 'disabled': False, 'in_flight': 0,
    'cooling': False, 'success_count': 236, 'err_total': 3, 'credits': 3002,
}
OUT_OF_POOL = {
    'uid': '99a07e71-e0c8-41af-a955-63ad678abcd2', 'nickname': '17748593599',
    'realm': 'cn',
}

# ── 第二个场景：把所有状态档都摆出来，用来核对**两页对同一个账号说法一致**。
# 每项：auth 文件里的账号 + 可选的池内状态（None = 不在池里 → 未加载）。
STATUS_CASES: list[tuple[dict, dict | None]] = [
    ({'uid': 'u-online', 'nickname': '正常号'}, {
        'uid': 'u-online', 'nickname': '正常号', 'healthy': True, 'disabled': False,
        'cooling': False, 'success_count': 120, 'err_total': 4, 'credits': 900}),
    ({'uid': 'u-cooling', 'nickname': '冷却号'}, {
        'uid': 'u-cooling', 'nickname': '冷却号', 'healthy': False, 'disabled': False,
        'cooling': True, 'cool_remaining_sec': 300, 'success_count': 50,
        'err_total': 9, 'credits': 500}),
    ({'uid': 'u-disabled', 'nickname': '禁用号'}, {
        'uid': 'u-disabled', 'nickname': '禁用号', 'healthy': False, 'disabled': True,
        'disabled_reason': 'request illegal (11140)', 'cooling': False,
        'success_count': 10, 'err_total': 30, 'credits': 100}),
    ({'uid': 'u-never', 'nickname': '一直失败号'}, {
        'uid': 'u-never', 'nickname': '一直失败号', 'healthy': False, 'disabled': False,
        'cooling': False, 'success_count': 0, 'err_total': 77, 'credits': 0}),
    # 文件在本地、上游没加载 → 「未加载」（与用户截图同一个形态）
    ({'uid': 'u-notloaded', 'nickname': '未加载号'}, None),
]


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
            self._json({'healthy': 1, 'total': 1})
        elif path == '/status':
            pool = [p for _a, p in STATUS_CASES if p] if STATUS_MODE else [IN_POOL]
            self._json({
                'accounts': pool,
                'total': len(pool), 'healthy': sum(1 for p in pool if p.get('healthy')),
                'cooling': sum(1 for p in pool if p.get('cooling')),
                'disabled': sum(1 for p in pool if p.get('disabled')),
                'in_flight_full': 0, 'redis_mode': 'noop', 'sticky_sessions': 0,
                'realm_totals': {
                    'cn': {'total': len(pool), 'healthy': sum(1 for p in pool if p.get('healthy')),
                           'cooling': sum(1 for p in pool if p.get('cooling')),
                           'disabled': sum(1 for p in pool if p.get('disabled')), 'in_flight_full': 0},
                    'global': {'total': 0, 'healthy': 0, 'cooling': 0, 'disabled': 0, 'in_flight_full': 0},
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


def write_auth(auth_dir: Path, account: dict) -> None:
    """写一个格式**合法**的 auth 文件（与上游落盘结构一致）。

    结构照 `wb2api.list_auth_accounts()` 读的字段：`account.uid/nickname`
    与 `auth.accessToken/expiresAt/domain`。accessToken 用带载荷的 JWT，
    这样剩余有效期、uid、昵称都能正常解析出来（否则会被标成「缺少 accessToken」，
    变成另一种场景，复现不出本问题）。

    文件本身完全正常，它不在池里纯粹是因为假上游的 /status 没返回它——
    这正是要复现的形态：**本地文件与上游池不一致**。
    """
    header = base64.urlsafe_b64encode(
        json.dumps({'alg': 'none', 'typ': 'JWT'}).encode()).decode().rstrip('=')
    exp = int(time.time()) + 60 * 86400
    payload = base64.urlsafe_b64encode(
        json.dumps({'iat': int(time.time()), 'exp': exp,
                    'uid': account['uid']}).encode()).decode().rstrip('=')
    token = f'{header}.{payload}.sig'
    doc = {
        'account': {'uid': account['uid'], 'nickname': account['nickname'],
                    'enterpriseId': 'repro-enterprise'},
        'auth': {'accessToken': token, 'refreshToken': 'fake-refresh-token',
                 'expiresAt': exp, 'domain': 'copilot.tencent.com'},
    }
    (auth_dir / f'workbuddy-{account["uid"]}.json').write_text(
        json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')


def main() -> int:
    # --all-status：摆出所有状态档（正常/冷却/禁用/一直失败/未加载），
    # 用来核对两页对同一个账号的说法是否逐字一致。
    global STATUS_MODE
    STATUS_MODE = '--all-status' in sys.argv

    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    auth_dir = DATA / 'auths'
    auth_dir.mkdir(parents=True)

    if STATUS_MODE:
        for account, _pool in STATUS_CASES:
            write_auth(auth_dir, account)
        pool_names = [p['nickname'] for _a, p in STATUS_CASES if p]
        print(f'假上游: http://127.0.0.1:{UPSTREAM_PORT}（池内 {len(pool_names)} 个: {pool_names}）')
        print(f'auths/ 下 {len(STATUS_CASES)} 个文件，其中 {len(STATUS_CASES) - len(pool_names)} 个不在池里')
    else:
        for acc in (IN_POOL, OUT_OF_POOL):
            write_auth(auth_dir, acc)
        print(f'假上游: http://127.0.0.1:{UPSTREAM_PORT}（池内仅 {IN_POOL["nickname"]}）')
        print(f'auths/ 下两个文件: {IN_POOL["nickname"]}, {OUT_OF_POOL["nickname"]}')

    server = ThreadingHTTPServer(('127.0.0.1', UPSTREAM_PORT), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        'WB2API_BASE': f'http://127.0.0.1:{UPSTREAM_PORT}',
        'WB_AUTH_DIR': str(auth_dir),
        'WB_DB': str(DATA / 'manager.db'),
        'WB_USERS_FILE': str(DATA / 'users.json'),
        'WB_MANAGER_HOST': '127.0.0.1',
        'WB_MANAGER_PORT': str(MANAGER_PORT),
        'WB_ADMIN_PASSWORD': ADMIN_PW,
    }
    print(f'假上游: http://127.0.0.1:{UPSTREAM_PORT}（池内仅 {IN_POOL["nickname"]}）')
    print(f'auths/ 下两个文件: {IN_POOL["nickname"]}, {OUT_OF_POOL["nickname"]}')
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin / {ADMIN_PW})')
    print('对照首页「账号健康快照」与「账号」页状态列 —— 两处应说法一致。Ctrl+C 退出。')
    try:
        subprocess.run(
            [sys.executable, '-m', 'uvicorn', 'server.main:app',
             '--host', '127.0.0.1', '--port', str(MANAGER_PORT)],
            cwd=str(REPO), env=env, check=False,
        )
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
