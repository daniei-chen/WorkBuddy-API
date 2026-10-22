"""网关流式路径：上游报错时日志要记下原因。

发现过程（2026-09-16 发版前核对上游）：上游 `5755fe3` 把错误响应改成了**原样透传**
（error.message 装上游 body 原文），并明确「上游错误码/账号语义允许对客户端可见，
排查必须看到原文」。透传本身没问题，但暴露了我们这一侧的缺陷：

网关流式分支里取错误体的写法是「缓冲超过 4000 字节才取一次」：

    if len(pending) > 4000:
        error_text = pending[:500]

而上游的错误体通常只有**几百字节**（实测 no_healthy_account 文案 111 字节），
于是 `error_text` 恒为 None → `db._clean(None)` 存成空串 →
**客户端看得到错误，管理端日志的 error 列却是空的**。用户来问「为什么失败」时
没有任何线索，只能靠复现。

anthropic 层早就修掉了同一个写法（见其注释：「早先要等到 4000 字节才取，而常见的
上游错误体只有几百字节 → error_text 恒为 None，错误被静默丢弃」），gateway 层漏了。

判据：上游 4xx/5xx + 小错误体 → 请求日志的 error 必须非空且含上游原文。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402

# 真实上游 503 的错误体大小量级（约 111 字节），刻意远小于 4000
UPSTREAM_ERROR = (
    b'{"error":{"message":"all accounts are temporarily unavailable",'
    b'"type":"api_error","code":"no_healthy_account"}}'
)


class _Resp:
    status_code = 503
    headers = {'content-type': 'application/json'}
    text = UPSTREAM_ERROR.decode()

    def json(self):
        import json
        return json.loads(UPSTREAM_ERROR)

    async def aiter_bytes(self):
        yield UPSTREAM_ERROR

    async def aread(self):
        return UPSTREAM_ERROR

    async def aclose(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Client:
    def build_request(self, *a, **kw):
        return object()

    async def send(self, req, stream=True):
        return _Resp()

    async def post(self, url, **kw):
        return _Resp()

    async def aclose(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class StreamErrorLoggingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 's.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        self.token = keysvc.create_key('t')['key']

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _chat(self, *, stream: bool):
        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            return self.client.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'model': 'glm-5.2',
                      'messages': [{'role': 'user', 'content': 'hi'}],
                      'stream': stream},
            )

    def _last_error(self) -> str:
        return db.query_one('SELECT error FROM request_logs ORDER BY id DESC LIMIT 1')['error'] or ''

    def test_stream_small_error_body_is_recorded(self) -> None:
        """核心判据：小错误体（<4000 字节）也必须落进日志。"""
        r = self._chat(stream=True)
        self.assertEqual(r.status_code, 503, r.text)
        # 客户端侧：原样透传（上游 5755fe3 的意图）
        self.assertIn(b'all accounts are temporarily unavailable', r.content)
        recorded = self._last_error()
        self.assertTrue(recorded.strip(), '客户端看得到错误，日志却什么都没记')
        self.assertIn('all accounts are temporarily unavailable', recorded)

    def test_non_stream_error_body_is_recorded(self) -> None:
        r = self._chat(stream=False)
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn('all accounts are temporarily unavailable', self._last_error())

    def test_stream_and_non_stream_agree(self) -> None:
        """两条路的日志口径要一致——同一份上游错误不该只在一侧留痕。"""
        self._chat(stream=True)
        streamed = self._last_error()
        self._chat(stream=False)
        plain = self._last_error()
        self.assertTrue(streamed.strip() and plain.strip(),
                        f'有路径没记录错误：stream={streamed!r} nonstream={plain!r}')
        self.assertIn('all accounts are temporarily unavailable', streamed)
        self.assertIn('all accounts are temporarily unavailable', plain)


if __name__ == '__main__':
    unittest.main()
