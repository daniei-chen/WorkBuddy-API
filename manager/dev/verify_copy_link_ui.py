"""复制链接「提示与实际一致」的验收（issue #57）——真服务 + 真浏览器。

现象：添加账号弹窗里点「复制链接」，提示「已复制到剪贴板」，但粘贴出来是空的。

根因：回退路径 `document.execCommand('copy')` 的**布尔返回值被忽略** —— 它被浏览器
拒绝时返回 false，而代码照样 `return true`，于是界面报成功。非安全上下文（普通
HTTP 访问）下 `navigator.clipboard` 是 undefined，走的正是这条回退路径，所以这类
部署最容易踩到。

这个脚本起一个真管理端（把 `start_login` 换成返回固定授权链接的假实现），再交给
`dev/verify_copy_link.mjs` 在两种环境下点「复制链接」：
  ① 有 Clipboard API；② 摘掉 Clipboard API 强制走回退路径。
不变量：**只要提示成功，剪贴板里就必须真有那条链接。**

    python dev/verify_copy_link_ui.py

数据落在 dev/.copy-link/（已 gitignore），截图在 dev/.shots-copy-link/。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / 'dev' / '.copy-link'
SHOTS = REPO / 'dev' / '.shots-copy-link'
UPSTREAM_PORT = 7941
MANAGER_PORT = 7942
ADMIN_PW = 'copy-link-pass'
FAKE_URL = 'https://example.invalid/authorize?state=copy-link-1'


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        import json
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json; charset=utf-8')
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/healthz', '/status'):
            self._json({'healthy': 1, 'total': 1, 'accounts': [],
                        'realm_totals': {'cn': {'total': 1}, 'global': {'total': 0}}})
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


def _playwright_entry() -> str | None:
    for base in (Path(tempfile.gettempdir()), Path(os.environ.get('TEMP') or '')):
        p = base / 'wb-i18n-verify' / 'node_modules' / 'playwright-core' / 'index.js'
        if p.is_file():
            return str(p)
    return None


def main() -> int:
    for d in (DATA, SHOTS):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    DATA.mkdir(parents=True)
    (DATA / 'auths').mkdir()

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
    # 在管理端进程里把「开始登录」换成返回固定授权链接的假实现（真实腾讯无法在
    # 测试里走完授权）；poll 保持「等待中」，弹窗停在可复制链接的状态即可。
    # 注意两个 fake 都必须是 **async**（调用方是 `await tencent.start_login(...)`，
    # 返回 dict 会报 `object dict can't be used in 'await' expression`）。
    launcher = (
        'from server.services import tencent\n'
        'async def fake_start(*a, **k):\n'
        f'    return {{"state": "copy-link-1", "authUrl": {FAKE_URL!r}}}\n'
        'async def fake_poll(*a, **k):\n'
        '    return {"status": "waiting", "message": "等待授权"}\n'
        'tencent.start_login = fake_start\n'
        'tencent.poll_login = fake_poll\n'
        'import server.routers.accounts as acc\n'
        'acc.tencent.start_login = fake_start\n'
        'acc.tencent.poll_login = fake_poll\n'
        'import uvicorn, server.main\n'
        f'uvicorn.run(server.main.app, host="127.0.0.1", port={MANAGER_PORT}, log_level="warning")\n'
    )
    proc = subprocess.Popen([sys.executable, '-c', launcher], cwd=str(REPO), env=env)
    print(f'管理端: http://127.0.0.1:{MANAGER_PORT}  (admin/{ADMIN_PW})')
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
            'WB_USER': 'admin',
            'WB_PASS': ADMIN_PW,
            'WB_SHOTS': str(SHOTS),
            'PYTHONUTF8': '1',
            **({'WB_PLAYWRIGHT': _playwright_entry()} if _playwright_entry() else {}),
        }
        r = subprocess.run(['node', 'dev/verify_copy_link.mjs'], cwd=str(REPO),
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
        upstream.shutdown()


if __name__ == '__main__':
    sys.exit(main())
