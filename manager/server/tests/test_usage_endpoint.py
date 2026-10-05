"""按密钥用量查询（GET /v1/usage）回归测试。

覆盖：窗口边界（今天 / 近 7 天 / 近 30 天，均含今天）、密钥数据隔离、命中率
算法与无数据时 null、鉴权分支（缺失 / 无效 / 停用 / 过期）、**配额用尽的密钥
仍可查询**（刻意设计：配额被拒时恰恰最需要看用量）、响应不含实付字段。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402


def _day(offset: int) -> str:
    """相对今天 offset 天的日期串（负数 = 过去）。"""
    return time.strftime('%Y-%m-%d', time.localtime(time.time() + offset * 86400))


class UsageEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'u.db'
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

    def _key(self, **kw) -> dict:
        return keysvc.create_key('t', **kw)

    def _add_usage(self, key_id: int, day: str, *, req: int = 1, pt: int = 100,
                   ct: int = 50, hit: int = 0, miss: int = 0,
                   model: str = 'glm-5.3-flash') -> None:
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, '
            'completion_tokens, credit, realm, cache_hit_tokens, cache_miss_tokens) '
            'VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?, ?)',
            (day, key_id, model, req, pt, ct, 'cn', hit, miss),
        )

    def _get(self, token: str | None):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        return self.client.get('/v1/usage', headers=headers)

    def test_windows_boundaries_and_hit_rate(self) -> None:
        """窗口边界：-6 天在 7 天内、-7 天在 7 天外；-10 在 30 天内、-40 在外。"""
        k = self._key()
        kid = k['id']
        self._add_usage(kid, _day(0), pt=1000, ct=500, hit=1200, miss=300)
        self._add_usage(kid, _day(-3), pt=200, ct=100, hit=100, miss=100)
        self._add_usage(kid, _day(-6), pt=10, ct=0)
        self._add_usage(kid, _day(-7), pt=20, ct=0)
        self._add_usage(kid, _day(-10), pt=400, ct=0)
        self._add_usage(kid, _day(-40), pt=9999, ct=9999)
        r = self._get(k['key'])
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d['today']['requests'], 1)
        self.assertEqual(d['today']['tokens'], 1500)
        self.assertEqual(d['today']['hit_rate'], 0.8)
        self.assertEqual(d['last_7d']['requests'], 3)          # 0 / -3 / -6
        self.assertEqual(d['last_7d']['tokens'], 1810)
        self.assertEqual(d['last_7d']['hit_rate'], round(1300 / 1700, 4))
        self.assertEqual(d['last_30d']['requests'], 5)         # + -7 / -10
        self.assertEqual(d['last_30d']['tokens'], 2230)
        self.assertEqual(d['last_30d']['hit_rate'], round(1300 / 1700, 4))
        self.assertEqual(d['key']['name'], 't')

    def test_no_data_null_hit_rate(self) -> None:
        """零调用：全 0，hit_rate 为 null（不是 0——0 会被读成“缓存全没命中”）。"""
        k = self._key()
        d = self._get(k['key']).json()
        for win in ('today', 'last_7d', 'last_30d'):
            self.assertEqual(d[win]['requests'], 0)
            self.assertEqual(d[win]['tokens'], 0)
            self.assertIsNone(d[win]['hit_rate'])

    def test_key_isolation(self) -> None:
        """只能看到自己的数据：B 的查询不含 A 的用量。"""
        a, b = self._key(), self._key()
        self._add_usage(a['id'], _day(0), pt=5000, ct=5000)
        d = self._get(b['key']).json()
        self.assertEqual(d['last_30d']['tokens'], 0)

    def test_auth_branches(self) -> None:
        self.assertEqual(self._get(None).status_code, 401)
        self.assertEqual(self._get('wbk_not_a_real_key').status_code, 401)
        k = self._key()
        db.execute('UPDATE api_keys SET enabled = 0 WHERE id = ?', (k['id'],))
        self.assertEqual(self._get(k['key']).status_code, 403)
        exp = self._key(expires_at=int(time.time()) - 10)
        self.assertEqual(self._get(exp['key']).status_code, 403)

    def test_quota_exhausted_key_can_still_query(self) -> None:
        """配额用尽的密钥仍可查询（这是本接口与 chat 的鉴权口径差异所在）。"""
        k = self._key(quota=100)
        db.execute('UPDATE api_keys SET used_tokens = 100 WHERE id = ?', (k['id'],))
        self._add_usage(k['id'], _day(0), pt=100, ct=0)
        r = self._get(k['key'])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['today']['tokens'], 100)

    def test_response_has_no_credit(self) -> None:
        """对外不暴露实付：响应里任何层级都不出现 credit 字段。"""
        k = self._key()
        self._add_usage(k['id'], _day(0))
        body = self._get(k['key']).json()

        def walk(o) -> None:
            if isinstance(o, dict):
                for kk, vv in o.items():
                    self.assertNotIn('credit', kk.lower())
                    walk(vv)
            elif isinstance(o, list):
                for vv in o:
                    walk(vv)

        walk(body)


    def test_live_speed(self) -> None:
        """实时速度：默认 3 秒窗口，只统计窗口内完成的生成 token。"""
        k = self._key()
        now = int(time.time())
        for ts_off, ct in ((0, 600), (-60, 9999)):
            db.execute(
                'INSERT INTO request_logs(ts, key_id, ip, model, status, prompt_tokens, '
                'completion_tokens, latency_ms) VALUES(?,?,?,?,?,?,?,?)',
                (now + ts_off, k['id'], '1.2.3.4', 'glm-5.3-flash', 200, 0, ct, 1000),
            )
        d = self._get(k['key']).json()
        self.assertEqual(d['live']['window_seconds'], 3)
        self.assertEqual(d['live']['tokens'], 600)
        self.assertEqual(d['live']['tokens_per_second'], 200.0)

    def test_live_idle_zero(self) -> None:
        """无活动时速度为 0（而不是 null）：0 本身就是有效信息。"""
        k = self._key()
        d = self._get(k['key']).json()
        self.assertEqual(d['live']['tokens'], 0)
        self.assertEqual(d['live']['tokens_per_second'], 0.0)

    def test_live_window_param(self) -> None:
        """live_window 参数：clamp 到 2~300，非法值回落默认 3。"""
        k = self._key()
        h = {'Authorization': f"Bearer {k['key']}"}
        db.execute(
            'INSERT INTO request_logs(ts, key_id, ip, model, status, prompt_tokens, '
            'completion_tokens, latency_ms) VALUES(?,?,?,?,?,?,?,?)',
            (int(time.time()), k['id'], '1.2.3.4', 'glm-5.3-flash', 200, 0, 50, 500),
        )
        d = self.client.get('/v1/usage?live_window=2', headers=h).json()
        self.assertEqual(d['live']['window_seconds'], 2)
        self.assertEqual(d['live']['tokens_per_second'], 25.0)
        self.assertEqual(self.client.get('/v1/usage?live_window=999', headers=h).json()['live']['window_seconds'], 300)
        self.assertEqual(self.client.get('/v1/usage?live_window=1', headers=h).json()['live']['window_seconds'], 2)
        self.assertEqual(self.client.get('/v1/usage?live_window=abc', headers=h).json()['live']['window_seconds'], 3)

    def test_by_model_breakdown(self) -> None:
        """Preserve local A2 contract: canonical models include every time window."""
        k = self._key()
        self._add_usage(k['id'], _day(0), pt=1000, ct=0, hit=800, miss=200, model='glm-5.3-flash')
        self._add_usage(k['id'], _day(-1), pt=500, ct=0, model='deepseek-v4.1-flash')
        self._add_usage(k['id'], _day(-40), pt=9999, ct=0, model='old-model')
        h = {'Authorization': f"Bearer {k['key']}"}
        d = self.client.get('/v1/usage', headers=h).json()
        self.assertIsInstance(d['by_model'], dict)
        self.assertEqual(d['by_model']['glm-5.3-flash']['today']['tokens'], 1000)
        d = self.client.get('/v1/usage?by_model=1', headers=h).json()
        rows = {model: windows['last_30d'] for model, windows in d['by_model'].items()}
        self.assertEqual(rows['glm-5.3-flash']['tokens'], 1000)
        self.assertEqual(rows['glm-5.3-flash']['hit_rate'], 0.8)
        self.assertEqual(rows['deepseek-v4.1-flash']['tokens'], 500)
        self.assertIsNone(rows['deepseek-v4.1-flash']['hit_rate'])
        self.assertNotIn('old-model', rows)

if __name__ == '__main__':
    unittest.main()
