"""上游 `error.gateway_hint` 的透传（上游 2026-09-16 新增）。

背景：上游给错误响应加了一个与 message **并列**的 `gateway_hint` 字段——网关视角
的**可执行建议**，只在该形态有明确动作时给（例如「no healthy account available in
pool; check /status or retry later」），未覆盖的形态不带。

`/v1/chat/completions` 本来就是**原样透传**上游响应，所以那条路天然带着它。问题在
两个**协议翻译层**：它们重建错误体时只取 `message`，这个字段会被丢掉——于是同一份
上游错误，走 Chat Completions 的客户端看得到建议、走 Anthropic Messages 或
Responses 的看不到。

这与 issue #18 是同一条原则的另一面：**真实原因（message）与可执行建议（hint）
都要能到达客户端**。丢掉 hint 的后果不算致命（客户端仍能看到 message），但它恰好
是「我该怎么办」那一句，丢了就只剩「出错了」。

判据：
  1. `_error_hint()` 取值正确，异常输入一律 None；
  2. 三个协议的错误体在**有 hint 时带上、无 hint 时不写该字段**（不编造）；
  3. 流式路径中途收到 error 帧时，hint 不能丢（SSE/事件流只有 message 一个字段位，
     所以要拼进去）。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import anthropic, gateway, responses  # noqa: E402

HINT = 'no healthy account available in pool; check /status or retry later'
BODY_WITH_HINT = {'error': {'message': 'all accounts busy', 'type': 'api_error',
                            'code': 'no_healthy_account', 'gateway_hint': HINT}}
BODY_NO_HINT = {'error': {'message': 'all accounts busy', 'type': 'api_error',
                          'code': 'no_healthy_account'}}


class ErrorHintExtractTest(unittest.TestCase):
    def test_reads_hint(self) -> None:
        self.assertEqual(gateway._error_hint(BODY_WITH_HINT), HINT)

    def test_missing_or_bad_shapes_are_none(self) -> None:
        """取不到一律 None —— 不能瞎编一个 hint 出来。"""
        for bad in (None, '', 'x', [], 0,
                    BODY_NO_HINT,
                    {'error': {}},
                    {'error': 'plain string'},
                    {'error': {'gateway_hint': ''}},
                    {'error': {'gateway_hint': '   '}},
                    {'error': {'gateway_hint': 123}},
                    {'error': {'gateway_hint': None}}):
            self.assertIsNone(gateway._error_hint(bad), repr(bad))

    def test_truncates_absurdly_long_hint(self) -> None:
        """上游给超长内容时不原样放大（与其它外发字段同口径）。"""
        got = gateway._error_hint({'error': {'gateway_hint': 'x' * 5000}})
        self.assertIsNotNone(got)
        self.assertLessEqual(len(got or ''), 300)


class ErrorEnvelopeTest(unittest.TestCase):
    """三个协议的错误体：有 hint 带上、无 hint 不写字段。"""

    def _json(self, resp: object) -> dict:
        return json.loads(resp.body.decode())  # type: ignore[attr-defined]

    def test_openai_envelope(self) -> None:
        with_hint = self._json(gateway._oai_error('m', 503, 'api_error', 'c', HINT))
        self.assertEqual(with_hint['error']['gateway_hint'], HINT)
        self.assertEqual(with_hint['error']['message'], 'm', 'message 必须原样保留')
        without = self._json(gateway._oai_error('m', 503, 'api_error', 'c'))
        self.assertNotIn('gateway_hint', without['error'], '无 hint 时不该写这个字段')

    def test_anthropic_envelope(self) -> None:
        with_hint = self._json(anthropic._err('m', 503, 'api_error', HINT))
        self.assertEqual(with_hint['error']['gateway_hint'], HINT)
        self.assertEqual(with_hint['error']['message'], 'm')
        self.assertEqual(with_hint['type'], 'error', 'Anthropic 外层结构不能变')
        without = self._json(anthropic._err('m'))
        self.assertNotIn('gateway_hint', without['error'])

    def test_responses_reuses_openai_envelope(self) -> None:
        with_hint = self._json(responses._failed('m', 503, 'api_error', 'c', HINT))
        self.assertEqual(with_hint['error']['gateway_hint'], HINT)
        without = self._json(responses._failed('m', 503))
        self.assertNotIn('gateway_hint', without['error'])


class StreamingErrorKeepsHintTest(unittest.TestCase):
    """流式的中途 error 帧：SSE/事件流只有一个 message 字段位，hint 必须拼进去。"""

    def test_anthropic_midstream_hint_is_not_dropped(self) -> None:
        """调用真实流式端点，喂一个带 hint 的 error 帧，确认建议出现在报错里。"""
        import tempfile
        from unittest import mock
        from fastapi.testclient import TestClient
        from server import config, db, keysvc, security

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        orig_db, orig_users = config.DB_PATH, config.USERS_FILE
        self.addCleanup(setattr, config, 'DB_PATH', orig_db)
        self.addCleanup(setattr, config, 'USERS_FILE', orig_users)
        config.DB_PATH = Path(tmp.name) / 'h.db'
        config.USERS_FILE = Path(tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        self.addCleanup(lambda: db._conn and db._conn.close())
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        client = TestClient(app)
        token = keysvc.create_key('t')['key']

        upstream = (b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                    + f'data: {json.dumps(BODY_WITH_HINT, ensure_ascii=False)}\n\n'.encode())

        class _Resp:
            status_code = 200

            async def aiter_bytes(self):
                yield upstream

            async def aclose(self):
                return None

        class _Client:
            def build_request(self, *a, **kw):
                return object()

            async def send(self, req, stream=True):
                return _Resp()

            async def aclose(self):
                return None

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            r = client.post('/v1/messages', headers={'x-api-key': token},
                            json={'model': 'glm-5.2', 'max_tokens': 8,
                                  'messages': [{'role': 'user', 'content': 'hi'}],
                                  'stream': True})
        text = r.content.decode()
        self.assertIn('error', text, '中途 error 帧必须转成 error 事件')
        self.assertIn('check /status', text, 'gateway_hint 被丢掉了')
        self.assertIn('all accounts busy', text, 'message 原文也该在')


if __name__ == '__main__':
    unittest.main()
