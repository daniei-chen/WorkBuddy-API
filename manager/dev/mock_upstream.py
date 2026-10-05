"""本地开发用的 workbuddy2api 模拟服务。

真实 workbuddy2api 是 Go 服务（默认 127.0.0.1:7863）。本机开发管理端时，
用它顶上，即可看到仪表盘、设置页、模型列表的完整效果。

    python dev/mock_upstream.py            # 监听 127.0.0.1:7863
    WB2API_BASE=http://127.0.0.1:7863 ...  # 管理端按默认值即可

仅用于本地联调，请勿在生产环境运行。
"""
from __future__ import annotations

import json
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = [
    'glm-5.2',
    'glm-5.1',
    'glm-5v-turbo',
    'kimi-k2.7',
    'minimax-m3',
    'hy3',
    'hy3-preview',
]

# 模拟几个账号，便于查看账号页与仪表盘（uid 与演示 auth 文件名对应）
ACCOUNTS = [
    {'uid': '89374120', 'nickname': '黑天鹅', 'healthy': True, 'disabled': False,
     'in_flight': 0, 'cooling': False, 'success_count': 4128, 'err_total': 6, 'credits': 1300},
    {'uid': '91203877', 'nickname': '测试号', 'healthy': True, 'disabled': False,
     'in_flight': 0, 'cooling': False, 'success_count': 236, 'err_total': 11, 'credits': 148},
    {'uid': '88110234', 'nickname': '运营小组', 'healthy': True, 'disabled': False,
     'in_flight': 1, 'cooling': False, 'success_count': 1873, 'err_total': 4, 'credits': 2600},
    {'uid': '87120988', 'nickname': '备用账号', 'healthy': False, 'disabled': False,
     'in_flight': 0, 'cooling': True, 'success_count': 96, 'err_total': 23, 'credits': 0},
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # 降低噪音
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split('?')[0]
        if path == '/healthz':
            self._json({'healthy': 1, 'total': 2, 'service': 'workbuddy2api'})
        elif path == '/status':
            self._json({
                'accounts': ACCOUNTS,
                'cooling': 1,
                'disabled': 0,
                'healthy': 3,
                'in_flight_full': 0,
                'redis_mode': 'noop',
                'sticky_sessions': 2,
                'total': 4,
            })
        elif path == '/v1/models':
            now = int(time.time())
            self._json({
                'object': 'list',
                'data': [
                    {'id': m, 'object': 'model', 'created': now, 'owned_by': 'workbuddy'}
                    for m in MODELS
                ],
            })
        else:
            self._json({'error': 'not found'}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        path = self.path.split('?')[0]
        if path in ('/v1/chat/completions', '/v2/chat/completions'):
            try:
                req = json.loads(raw or b'{}')
            except Exception:
                req = {}
            model = req.get('model') or 'glm-5.2'
            # 模拟真实上游的耗时波动，便于本地观察延迟分布
            time.sleep(random.uniform(0.35, 1.8))
            prompt_tokens = random.randint(80, 1600)
            completion_tokens = random.randint(20, 900)
            self._json({
                'id': 'chatcmpl-mock',
                'object': 'chat.completion',
                'created': int(time.time()),
                'model': model,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'pong'}, 'finish_reason': 'stop'}],
                'usage': {
                    'prompt_tokens': prompt_tokens,
                    'completion_tokens': completion_tokens,
                    'total_tokens': prompt_tokens + completion_tokens,
                },
            })
        else:
            self._json({'ok': True})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 7863), Handler)
    print('mock workbuddy2api -> http://127.0.0.1:7863  (Ctrl+C 退出)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
