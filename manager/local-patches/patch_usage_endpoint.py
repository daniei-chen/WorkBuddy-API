#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地补丁：新增按密钥用量查询端点 GET /v1/usage（gateway.py）+ 回归测试。

背景（2026-09-23）：给密钥持有者一个自助查用量的入口——当天 / 近 7 天 /
近 30 天的请求数、Token 用量与缓存命中率；**不含实付**。鉴权刻意比 chat 轻：
配额用尽的密钥也能查（那一刻恰恰最需要看用量）。数据口径与 /api/stats 同源
（同一张 usage_daily、同一套 day 过滤）。

锚点：在 `@router.get('/v1/models')` 之前插入；锚点必须恰好命中 1 次，
否则中止不写（fail-closed，宁可不打也不写坏）。两代 gateway.py 均有该路由。
幂等：检测到 `@router.get('/v1/usage')` 即跳过。
用法：patch_usage_endpoint.py [gateway.py 路径]
"""
from __future__ import annotations

import os
import shutil
import sys
import time

DEFAULT_GATEWAY = '/opt/workbuddy-manager/server/routers/gateway.py'
TEST_FILE = '/opt/workbuddy-manager/server/tests/test_usage_endpoint.py'
BACKUP = '/opt/workbuddy-manager/local-patches/backup'

ANCHOR = "@router.get('/v1/models')"
MARK = "@router.get('/v1/usage')"

BLOCK = '''# ── 按密钥用量查询 /v1/usage ──────────────────────────────────────────────
#
# 给密钥持有者一个自助查用量的入口：带自己的密钥即可查当天 / 近 7 天 / 近 30 天
# 的请求数、Token 用量与缓存命中率。**不返回实付（credit）**——那是内部口径，
# 对外只回答"用了多少"。
#
# 鉴权刻意比 chat 轻：配额用尽的密钥也能查。配额打满时 chat 会被 429 拒绝，
# 而"我用了多少、还剩多少"的答案只能从本接口拿——此时反而是最需要它的时刻。
# 所以不走 _authorize 的完整链路（其中 keysvc.validate 会把配额用尽直接拦掉），
# 只保留"密钥本身有效"这一层：resolve + 停用 / 过期。
# 数据口径与 /api/stats 完全一致（同一张 usage_daily 表、同一套 day 过滤），
# 保证"用户自查"与"控制台看到"的数字一致。


def _usage_since(days: int) -> str:
    """近 days 天（含今天）的起始日期串——口径与 /api/stats 的 _since 一致。"""
    return time.strftime('%Y-%m-%d', time.localtime(time.time() - (days - 1) * 86400))


def _usage_window(key_id: int, since_day: str) -> dict:
    """聚合单个密钥自 since_day 起的用量（含当天）。

    hit_rate 分母为 0（无调用 / 上游从未返回缓存字段）时给 null：
    0 会被读成"缓存全没命中"，与"没有数据"是两回事。
    """
    row = db.query_one(
        'SELECT COALESCE(SUM(requests),0) AS r, '
        'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t, '
        'COALESCE(SUM(cache_hit_tokens),0) AS hit, '
        'COALESCE(SUM(cache_miss_tokens),0) AS miss '
        'FROM usage_daily WHERE key_id = ? AND day >= ?',
        (key_id, since_day),
    )
    hit = int((row['hit'] if row is not None else 0) or 0)
    miss = int((row['miss'] if row is not None else 0) or 0)
    cached = hit + miss
    return {
        'requests': int((row['r'] if row is not None else 0) or 0),
        'tokens': int((row['t'] if row is not None else 0) or 0),
        'cache_hit_tokens': hit,
        'cache_miss_tokens': miss,
        'hit_rate': round(hit / cached, 4) if cached else None,
    }


def _usage_live(key_id: int, window: int = 3) -> dict:
    """实时速度：最近 window 秒完成的生成 token 速率（客户端轮询即可看到跳动）。

    取 request_logs 而不是 usage_daily：后者按天聚合，颗粒度不够。
    只计 completion_tokens（输出速度才是"token 速度"的直觉口径）；
    窗口内没有请求时速率自然归零——0 本身就是有意义的信息。
    """
    since = int(time.time()) - window
    row = db.query_one(
        'SELECT COALESCE(SUM(completion_tokens),0) AS t FROM request_logs '
        'WHERE key_id = ? AND ts >= ?',
        (key_id, since),
    )
    tokens = int((row['t'] if row is not None else 0) or 0)
    return {
        'window_seconds': window,
        'tokens': tokens,
        'tokens_per_second': round(tokens / window, 1),
    }


def _usage_by_model(key_id: int, since_day: str) -> list[dict]:
    """按模型拆分用量（近 N 天，含当天）。口径与 _usage_window 一致，按 tokens 降序。"""
    rows = db.query(
        "SELECT COALESCE(model, '未知') AS name, "
        'COALESCE(SUM(requests),0) AS r, '
        'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t, '
        'COALESCE(SUM(cache_hit_tokens),0) AS h, '
        'COALESCE(SUM(cache_miss_tokens),0) AS m '
        'FROM usage_daily WHERE key_id = ? AND day >= ? GROUP BY model '
        'ORDER BY t DESC',
        (key_id, since_day),
    )
    out = []
    for row in rows:
        hit, miss = int(row['h'] or 0), int(row['m'] or 0)
        cached = hit + miss
        out.append({
            'model': row['name'],
            'requests': int(row['r'] or 0),
            'tokens': int(row['t'] or 0),
            'cache_hit_tokens': hit,
            'cache_miss_tokens': miss,
            'hit_rate': round(hit / cached, 4) if cached else None,
        })
    return out


@router.get('/v1/usage')
async def usage_v1(request: Request):
    """按密钥查询用量：当天 / 近 7 天 / 近 30 天。

    调用方式：Authorization: Bearer <wbk_密钥>（也认 x-api-key 头）。
    返回 tokens = prompt + completion 合计；hit_rate = 缓存命中 token 占比，
    0~1 小数（无数据时为 null）；含义与 /api/stats 页面同源。
    live_window 查询参数可调实时速度窗口（秒，默认 3，钳制 2~300）；
    by_model=1 时额外返回近 30 天按模型拆分（by_model 数组）。
    """
    token = _bearer(request)
    if not token:
        return _oai_error('缺少 API Key，请在 Authorization 头中提供 Bearer 令牌', 401,
                          'authentication_error', 'missing_api_key')
    key = keysvc.resolve(token)
    if not key:
        return _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')
    if not key['enabled']:
        return _oai_error('密钥已停用', 403, 'permission_error', 'key_disabled')
    if key['expires_at'] and key['expires_at'] < time.time():
        return _oai_error('密钥已过期', 403, 'permission_error', 'key_expired')
    today = time.strftime('%Y-%m-%d')
    # 实时速度窗口可调（秒）：默认 3 秒——请求完成瞬间数字尖跳，轮询越快越动感。
    # 客户端轮询间隔建议不超过窗口的一半，否则会踩空看不到跳（见 _usage_live 文档）。
    try:
        live_window = int(request.query_params.get('live_window', '3'))
    except (TypeError, ValueError):
        live_window = 3
    live_window = min(300, max(2, live_window))
    resp = {
        'key': {'name': key['name'], 'prefix': key['prefix']},
        'today': _usage_window(key['id'], today),
        'last_7d': _usage_window(key['id'], _usage_since(7)),
        'last_30d': _usage_window(key['id'], _usage_since(30)),
        'live': _usage_live(key['id'], live_window),
    }
    # by_model=1：额外给出近 30 天按模型拆分（用于客户端展示"哪个模型用了多少"）
    if request.query_params.get('by_model') in ('1', 'true', 'yes'):
        resp['by_model'] = _usage_by_model(key['id'], _usage_since(30))
    return resp'''

TEST_SRC = '''"""按密钥用量查询（GET /v1/usage）回归测试。

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
        """by_model=1：近 30 天按模型拆分；默认响应不含该字段；30 天外不出现。"""
        k = self._key()
        self._add_usage(k['id'], _day(0), pt=1000, ct=0, hit=800, miss=200, model='glm-5.3-flash')
        self._add_usage(k['id'], _day(-1), pt=500, ct=0, model='deepseek-v4.1-flash')
        self._add_usage(k['id'], _day(-40), pt=9999, ct=0, model='old-model')
        h = {'Authorization': f"Bearer {k['key']}"}
        d = self.client.get('/v1/usage', headers=h).json()
        self.assertNotIn('by_model', d)
        d = self.client.get('/v1/usage?by_model=1', headers=h).json()
        rows = {r['model']: r for r in d['by_model']}
        self.assertEqual(rows['glm-5.3-flash']['tokens'], 1000)
        self.assertEqual(rows['glm-5.3-flash']['hit_rate'], 0.8)
        self.assertEqual(rows['deepseek-v4.1-flash']['tokens'], 500)
        self.assertIsNone(rows['deepseek-v4.1-flash']['hit_rate'])
        self.assertNotIn('old-model', rows)

if __name__ == '__main__':
    unittest.main()
'''


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_GATEWAY
    if os.path.exists(src):
        s = open(src, encoding='utf-8').read()
        if MARK in s:
            print('usage 端点已存在，跳过')
        elif s.count(ANCHOR) != 1:
            print(f'ERROR: 锚点命中 {s.count(ANCHOR)} 次（应为 1），中止不写')
        else:
            os.makedirs(BACKUP, exist_ok=True)
            if not any(f.startswith('gateway.py.orig-usage-endpoint') for f in os.listdir(BACKUP)):
                bak = os.path.join(BACKUP, 'gateway.py.orig-usage-endpoint-' + time.strftime('%Y%m%d-%H%M%S'))
                shutil.copy2(src, bak)
                print(f'备份原文件 -> {bak}')
            out = s.replace(ANCHOR, BLOCK.rstrip() + '\n\n\n' + ANCHOR, 1)
            open(src, 'w', encoding='utf-8').write(out)
            print('已插入 /v1/usage 端点')
    else:
        print(f'ERROR: {src} 不存在，跳过')
    if os.path.exists(TEST_FILE):
        print('测试文件已存在，跳过')
    else:
        os.makedirs(os.path.dirname(TEST_FILE), exist_ok=True)
        open(TEST_FILE, 'w', encoding='utf-8').write(TEST_SRC)
        print(f'已写入 {TEST_FILE}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
