"""流式错误透传的回归测试（端到端，含假上游）。

**为什么单独一个文件**：PR 自带的用例全是纯函数单测（翻译函数），HTTP 端点
只覆盖了鉴权。而这里要钉住的两个缺陷都只在**真实链路**上才暴露——把
`/v1/messages` 的流式分支和假上游接起来跑，才看得到。

两个缺陷都是同一个后果：**上游报错，客户端收到「成功但内容为空」**，
且拿不到任何错误信息（对 Claude Code 这类客户端表现为模型静默返回空回复，
而不是报错——比报错更难排查）。

  1. 错误体收集条件写反了：`if len(pending) > 4000`。真实的上游错误体只有
     一二百字节（OpenAI 风格 error JSON），永远进不去那个分支，`error_text`
     恒为 None。
  2. 事件顺序：`error` 被发在 `message_stop` **之后**。多数 SDK 把
     `message_stop` 当流的终止信号，读到就结束迭代——那个 error 永远看不到。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _sse(obj) -> str:
    """拼一个 SSE 帧（`data: {...}` + 空行分隔）。"""
    return 'data: ' + json.dumps(obj) + '\n\n'


class StreamErrorPassthroughTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from server import config, db, keysvc

        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = cls._dir / 'se.db'
        config.USERS_FILE = cls._dir / 'users.json'
        config.STATIC_DIR = cls._dir / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')

        db._conn = None
        db.connect()
        from server.main import app
        cls.key = keysvc.create_key(name='stream-error-test')['key']
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def _headers(self) -> dict:
        return {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}

    def _post_stream(self, upstream_status: int, upstream_body: str):
        """发一次流式请求，返回响应。上游用假对象替掉。"""
        from server import config

        class _Resp:
            def __init__(self) -> None:
                self.status_code = upstream_status
                self.headers = {'content-type': 'application/json'}
                self.text = upstream_body

            def json(self):
                return json.loads(upstream_body)

            async def aiter_bytes(self):
                yield upstream_body.encode()

            async def aclose(self):
                return None

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kw):
                return _Resp()

            def build_request(self, *a, **kw):
                return object()

            async def send(self, req, **kw):
                return _Resp()

            async def aclose(self):
                return None

        body = {'model': 'm', 'max_tokens': 10, 'stream': True,
                'messages': [{'role': 'user', 'content': 'hi'}]}
        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            return self.c.post('/v1/messages', json=body, headers=self._headers())

    # 上游错误的真实形态：OpenAI 风格，约 170 字节
    _SHORT_ERROR = json.dumps({
        'error': {
            'message': 'The model `gpt-4o` does not exist or you do not have access to it.',
            'type': 'invalid_request_error',
            'code': 'model_not_found',
        }
    })

    def test_short_upstream_error_reaches_client(self) -> None:
        """**核心回归**：一二百字节的错误体也必须透传（原来被 4000 阈值滤掉）。"""
        r = self._post_stream(400, self._SHORT_ERROR)
        self.assertIn('event: error', r.text,
                      '上游报错但客户端没收到 error 事件——这就是「静默空回复」的成因')
        self.assertIn('does not exist', r.text, '错误内容应透传，不能只给个笼统提示')

    def test_error_is_not_treated_as_success(self) -> None:
        """错误响应不能只给一套「成功收尾」的事件——那是客户端误判的来源。"""
        r = self._post_stream(400, self._SHORT_ERROR)
        self.assertIn('event: error', r.text)

    def test_error_precedes_message_stop(self) -> None:
        """**顺序**：error 必须在 message_stop 之前。

        多数 SDK 把 message_stop 当终止信号，读到就停止迭代——发在它之后的
        error 等于没发（客户端仍表现为「成功但空」）。
        """
        r = self._post_stream(400, self._SHORT_ERROR)
        i_err = r.text.find('event: error')
        i_stop = r.text.find('event: message_stop')
        self.assertNotEqual(i_err, -1, '没有 error 事件')
        self.assertNotEqual(i_stop, -1, '没有 message_stop（部分客户端会一直等）')
        self.assertLess(i_err, i_stop,
                        'error 发在 message_stop 之后——客户端读到 stop 就不再读了')

    def test_midstream_error_precedes_message_stop(self) -> None:
        """状态码 **200** 但中途回 error 帧：同样要 error 在前。

        与上面「上游直接报错」是同一原则的另一个入口——收尾逻辑只有一份，
        这条守住它没被改回「先 stop 后 error」。此处流已经开始（已吐过
        content），所以更容易被误认为「已经成功了，顺序无所谓」。
        """
        body = _sse({'choices': [{'delta': {'content': 'partial'},
                                  'finish_reason': None}]}) + \
               _sse({'error': {'message': 'upstream blew up mid-stream'}})
        r = self._post_stream(200, body)
        i_err = r.text.find('event: error')
        i_stop = r.text.find('event: message_stop')
        self.assertNotEqual(i_err, -1, '中途出错必须告诉客户端，否则只剩「空回复」')
        self.assertNotEqual(i_stop, -1, '仍要给出收尾事件，否则客户端会一直等')
        self.assertLess(i_err, i_stop, 'error 发在 message_stop 之后——客户端读到 stop 就不再读了')
        self.assertIn('upstream blew up', r.text, '错误内容要透传')

    def test_large_error_body_also_works(self) -> None:
        """大错误体（原来唯一能过的情形）不能被改坏。"""
        big = json.dumps({'error': {'message': 'x' * 9000}})
        r = self._post_stream(500, big)
        self.assertIn('event: error', r.text)

    def test_success_stream_has_no_error_event(self) -> None:
        """正常流不该凭空冒出 error 事件。"""
        ok_body = 'data: ' + json.dumps({
            'choices': [{'delta': {'content': 'hello'}, 'finish_reason': None}],
        }) + '\n\n' + 'data: ' + json.dumps({
            'choices': [{'delta': {}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2},
        }) + '\n\n'
        r = self._post_stream(200, ok_body)
        self.assertNotIn('event: error', r.text, '正常流不该有 error 事件')
        self.assertIn('event: message_start', r.text)
        self.assertIn('event: message_stop', r.text)
        self.assertIn('hello', r.text)


if __name__ == '__main__':
    unittest.main()
