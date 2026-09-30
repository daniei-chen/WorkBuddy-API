"""聊天测试台与签到记录合并的回归测试。

覆盖两处曾被反馈的问题：

1. **签到记录看不到上游自动签到**：签到记录原先只写本端触发的（手动/批量/
   添加账号），上游定时签到落在 task_logs，于是「自动签到」永远看不到。
   现把两者按时间归并返回，并分别给出 local_total / auto_total。

2. **测试台越权**：测试台会真实消耗积分，必须限定管理员——默认部署里有弱口令
   的 guest 账号，放开给所有登录用户等于把额度借出去。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db  # noqa: E402
from server.routers import accounts as accounts_router  # noqa: E402
from server.routers import playground as playground_router  # noqa: E402


class MergedCheckinLogs(unittest.TestCase):
    """签到记录 = 本端触发 + 上游自动签到。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        db._conn = None
        db.connect()
        self.now = int(time.time())

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _call(self, **kw):
        """直接调路由函数（绕开鉴权依赖）。"""
        params = {'limit': 200, 'uid': None, 'offset': 0, 'days': 7, 'user': {'username': 'admin'}}
        params.update(kw)
        with mock.patch.object(accounts_router, '_nickname_resolver',
                               return_value=lambda uid: ''):
            return accounts_router.checkin_logs(**params)

    def test_auto_checkin_appears_in_checkin_logs(self) -> None:
        """核心回归：上游自动签到必须出现在签到记录里。"""
        db.add_task_logs([{
            'ts': self.now - 60, 'uid': 'abcdef123456', 'kind': 'checkin',
            'level': 'info', 'credits': 0,
            'message': 'checkin done: total=3 ok=3 already=0 fail=0 skipped=0',
            'dedup_key': 'auto-1',
        }])
        out = self._call()
        self.assertEqual(out['auto_total'], 1)
        self.assertEqual(len(out['items']), 1)
        self.assertTrue(out['items'][0]['auto'])
        self.assertEqual(out['items'][0]['source'], 'auto')

    def test_local_and_auto_are_merged_by_time(self) -> None:
        # 本端记录写入即 now；自动那条设为 +5 秒后（未来一点点），
        # 以便断言「归并后按时间倒序」——否则两者同秒，顺序不确定。
        db.add_checkin_log('u1', '甲', 'manual', True, 0, '签到成功')
        db.add_task_logs([{
            'ts': self.now + 5, 'uid': 'u2', 'kind': 'checkin', 'level': 'info',
            'credits': 0, 'message': 'checkin done: total=1 ok=1', 'dedup_key': 'auto-2',
        }])
        out = self._call()
        self.assertEqual(out['total'], 2)
        self.assertEqual(out['local_total'], 1)
        self.assertEqual(out['auto_total'], 1)
        self.assertTrue(out['items'][0]['auto'], '更近的记录应排在前面')
        self.assertFalse(out['items'][1]['auto'])

    def test_other_kinds_not_pulled_in(self) -> None:
        """只合并 checkin，别把旅行/活跃混进签到记录。"""
        db.add_task_logs([
            {'ts': self.now, 'uid': 'u', 'kind': 'travel', 'level': 'credit',
             'credits': 9, 'message': 'travel arrived', 'dedup_key': 't1'},
            {'ts': self.now, 'uid': 'u', 'kind': 'activity', 'level': 'info',
             'credits': 0, 'message': 'activity report', 'dedup_key': 'a1'},
        ])
        out = self._call()
        self.assertEqual(out['auto_total'], 0)
        self.assertEqual(out['items'], [])

    def test_ids_do_not_collide_across_tables(self) -> None:
        """两表 id 各自从 1 开始，合并后必须唯一（否则前端 key 冲突）。"""
        db.add_checkin_log('u1', '甲', 'manual', True, 0, '签到成功')
        db.add_task_logs([{
            'ts': self.now, 'uid': 'u2', 'kind': 'checkin', 'level': 'info',
            'credits': 0, 'message': 'checkin done', 'dedup_key': 'a3',
        }])
        out = self._call()
        ids = [it['id'] for it in out['items']]
        self.assertEqual(len(ids), len(set(ids)), f'id 冲突: {ids}')

    def test_offset_paging(self) -> None:
        for i in range(5):
            db.add_checkin_log(f'u{i}', f'n{i}', 'manual', True, 0, f'ok{i}')
        page1 = self._call(limit=2, offset=0)
        page2 = self._call(limit=2, offset=2)
        self.assertEqual(len(page1['items']), 2)
        self.assertEqual(len(page2['items']), 2)
        self.assertEqual(page1['total'], 5)
        ids1 = {i['id'] for i in page1['items']}
        ids2 = {i['id'] for i in page2['items']}
        self.assertFalse(ids1 & ids2, '两页不应重复')

    def test_paging_beyond_500_does_not_lose_records(self) -> None:
        """候选量必须覆盖到当前页末尾。

        曾经的写法是各表固定取最近 500 条：合并后按时间排序，第 500 名之后的
        记录永远翻不到，而 total 报的是真实全量——界面会显示「共 800 条」
        却翻不出后面 300 条。这里用 600 条本端记录验证第 501 条仍能取到。
        """
        for i in range(600):
            db.add_checkin_log(f'u{i}', f'n{i}', 'manual', True, 0, f'ok{i}')
        # 本端记录是同一秒写入，按 id 倒序即时间倒序
        page = self._call(limit=10, offset=500)
        self.assertEqual(len(page['items']), 10, '第 500 条之后应仍能翻到')
        self.assertEqual(page['total'], 600)
        # offset 接近末尾时应收敛，而不是报错
        tail = self._call(limit=10, offset=595)
        self.assertEqual(len(tail['items']), 5)

    def test_offset_past_end_returns_empty(self) -> None:
        db.add_checkin_log('u1', '甲', 'manual', True, 0, 'ok')
        out = self._call(limit=10, offset=999)
        self.assertEqual(out['items'], [])
        self.assertEqual(out['total'], 1)

    def test_days_filter_applies_to_both(self) -> None:
        old = self.now - 40 * 86400
        db.add_checkin_log('u1', '甲', 'manual', True, 0, '很久以前')
        db.execute('UPDATE checkin_logs SET ts = ?', (old,))
        db.add_task_logs([{
            'ts': old, 'uid': 'u2', 'kind': 'checkin', 'level': 'info',
            'credits': 0, 'message': 'old auto', 'dedup_key': 'old',
        }])
        out = self._call(days=7)
        self.assertEqual(out['total'], 0, '超出时间窗的都应被排除')
        out_all = self._call(days=90)
        self.assertEqual(out_all['total'], 2)


class PlaygroundAuth(unittest.TestCase):
    """测试台必须仅管理员可用。

    必须**显式固定 USERS_FILE**：否则它会在「已存在的 data/users.json」与
    「bootstrap 生成」之间漂移，测试结果取决于运行环境（CI 无该文件、
    本地有），曾因此在 CI 上失败而本地通过。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig_db = config.DB_PATH
        cls._orig_users = config.USERS_FILE
        cls._orig_static = config.STATIC_DIR
        config.DB_PATH = Path(cls._tmp.name) / 'p.db'
        config.USERS_FILE = Path(cls._tmp.name) / 'users.json'
        config.STATIC_DIR = Path(cls._tmp.name) / 'nonexistent-static'
        import os
        os.environ.setdefault('WB_ADMIN_PASSWORD', 'test-admin-pw')
        # 显式写出该文件：不依赖 bootstrap 的副作用，也不依赖运行目录里有没有残留
        from server import security
        security.save_users({
            'secret': 'playground-test-secret',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('test-admin-pw')}],
            'api_keys': [],
        })
        db._conn = None
        db.connect()
        from server.main import app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = cls._orig_db
        config.USERS_FILE = cls._orig_users
        config.STATIC_DIR = cls._orig_static
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def test_requires_login(self) -> None:
        r = self.client.post('/api/playground/chat', json={
            'model': 'glm-5.2', 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertIn(r.status_code, (401, 403))

    def test_admin_passes_auth_and_fails_only_on_upstream(self) -> None:
        """管理员应通过鉴权；没有真上游时应在转发阶段报 502，而不是 401/403。"""
        c = TestClient(self.client.app)
        c.post('/api/login', json={'username': 'admin', 'password': 'test-admin-pw'})

        class FakeResp:
            status_code = 200
            headers = {'content-type': 'text/event-stream'}

            async def aiter_bytes(self):
                yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                yield b'data: [DONE]\n\n'

            async def aread(self):
                return b'{"choices":[]}'

            async def aclose(self):
                return None

        class FakeClient:
            def build_request(self, *a, **k):
                return object()

            async def send(self, *a, **k):
                return FakeResp()

            async def aclose(self):
                return None

        with mock.patch.object(config, 'http_client', return_value=FakeClient()), \
                mock.patch.object(playground_router.config, 'http_client', return_value=FakeClient()):
            r = c.post('/api/playground/chat', json={
                'model': 'glm-5.2', 'messages': [{'role': 'user', 'content': 'hi'}],
                'stream': True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('hi', r.text)


if __name__ == '__main__':
    unittest.main()

class PlaygroundAccounting(unittest.TestCase):
    """测试台必须记账：它真实消耗积分，不记录则「请求日志/用量」对不上。"""

    def setUp(self) -> None:
        import tempfile
        from server import config, db
        self._db = db
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'acc.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        from server import config
        if self._db._conn is not None:
            self._db._conn.close()
        self._db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_stream_call_is_logged_without_key(self) -> None:
        """流式调用应落一条 request_logs，key_id 为空（可据此识别为测试台）。"""
        import asyncio
        from server.routers import gateway, playground as pg

        # 用 chr(10) 拼换行：避免补丁脚本里的转义被 shell/heredoc 吞掉
        NL = chr(10)
        SSE = (
            b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}' + NL.encode()
            + NL.encode()
            + b'data: {"choices":[{"delta":{"content":"hi"}}]}' + NL.encode() + NL.encode()
            + b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,'
            b'"total_tokens":10,"credit":0.25}}' + NL.encode() + NL.encode()
            + b'data: [DONE]' + NL.encode() + NL.encode()
        )

        class FakeResp:
            status_code = 200
            headers = {'content-type': 'text/event-stream'}
            async def aiter_bytes(self):
                yield SSE
            async def aread(self):
                return SSE
            async def aclose(self):
                pass

        class FakeClient:
            def build_request(self, *a, **k):
                return object()
            async def send(self, *a, **k):
                return FakeResp()
            async def aclose(self):
                pass

        body = pg.ChatIn(model='glm-5.3', messages=[{'role': 'user', 'content': 'hi'}], stream=True)

        class FakeReq:
            headers = {'user-agent': 'pytest'}

        async def run():
            with mock.patch.object(pg.config, 'http_client', return_value=FakeClient()),                     mock.patch.object(pg.config, 'upstream_api_key', return_value='K'),                     mock.patch.object(pg, 'client_ip', return_value='10.0.0.9'):
                resp = await pg.chat(body, request=FakeReq(), user={'username': 'admin'})
                async for _ in resp.body_iterator:
                    pass

        asyncio.run(run())
        row = self._db.query_one(
            'SELECT key_id, model, prompt_tokens, completion_tokens, credit, first_token_ms '
            'FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertIsNotNone(row, '测试台调用必须留下日志')
        self.assertIsNone(row['key_id'], '测试台无对外密钥，key_id 应为空')
        self.assertEqual(row['model'], 'glm-5.3')
        self.assertEqual(row['prompt_tokens'], 7)
        self.assertEqual(row['completion_tokens'], 3)
        self.assertAlmostEqual(row['credit'], 0.25, places=6)
        self.assertIsNotNone(row['first_token_ms'], '首字延迟也应记录')



if __name__ == "__main__":
    unittest.main()
