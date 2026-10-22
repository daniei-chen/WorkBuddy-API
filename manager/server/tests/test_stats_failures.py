"""失败请求数必须能被统计到（服务器审计发现的可观测性缺口）。

现场（线上部署实测）：凌晨一次全池中断，**31 次请求全部失败**，而管理端
趋势图上显示为「没有请求」——用户只能去翻日志才知道出过事。

根因是统计的数据源：

  · 界面上所有用量数字（总览、趋势图）都来自 `usage_daily`；
  · 而 `usage_daily` **只累计有 token 或有扣费的请求**（`bump_usage` 的调用
    条件是 `if total or credit`，刻意如此——否则「只有被拒绝的调用」的部署
    会被 `_usage_health` 误报成「统计没在累计」）；
  · 失败请求恰好都是零 token：403/429（密钥被拒、配额、限流）、503（池里没
    可用账号）、502（上游不可用）。

于是失败请求在用量表里**根本不存在**，界面上那天的曲线是空的。

修法：失败数单独取自 `request_logs`（它无条件记录每一笔），不碰
`usage_daily` 的口径。本文件锁住三条性质：

  1. 失败数能算出来，且**零 token 的失败也算**（这正是关键）；
  2. 无密钥的 401 不计入（扫描器噪声，混进来会让信号失真）；
  3. **只有失败、没有成功的日子也要出现在按天聚合里** —— 那正是最该被看到
     的一天，若按用量表出行它会整条缺失。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import stats  # noqa: E402


def _insert_log(ts: int, kid: int | None, status: int,
                pt: int = 0, ct: int = 0, credit: float | None = None,
                realm: str = 'cn') -> None:
    db.execute(
        'INSERT INTO request_logs(ts,key_id,ip,model,mapped_model,status,prompt_tokens,'
        'completion_tokens,latency_ms,ua,error,stream,credit,realm) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (ts, kid, '1.2.3.4', 'deepseek-v4.1-flash', 'deepseek-v4.1-flash',
         status, pt, ct, 100, 'UA', None, 1, credit, realm),
    )


class FailuresCountedTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'f.db'
        db._conn = None
        db.connect()
        db.execute("INSERT INTO api_keys(name,key_hash,prefix,enabled,created_at,quota,used_tokens) "
                   "VALUES('k','h','wbk_test',1,?,0,0)", (int(time.time()),))
        self.kid = db.query_one('SELECT id FROM api_keys LIMIT 1')['id']

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def _summary(self, realm=None) -> dict:
        return stats.summary(realm=realm, user={'username': 'a', 'role': 'admin'})

    def _daily(self, days=30, realm=None) -> list[dict]:
        return stats.daily(days=days, realm=realm, user={'username': 'a', 'role': 'admin'})

    # ── 1. 零 token 的失败必须被算到 ────────────────────────────
    def test_zero_token_failures_are_counted(self) -> None:
        """**核心断言**：503/429/403 这些零 token 的失败必须出现在失败数里。

        它们是用量表看不见的部分（`bump_usage` 不累计零 token 请求），
        也正是「全池中断」这种事故的形态。
        """
        now = int(time.time())
        for st in (503, 503, 503, 429, 403, 502):
            _insert_log(now, self.kid, st)
        f = self._summary()['failures']
        self.assertEqual(f['today_5xx'], 4, '503×3 + 502×1 应计 4 次 5xx')
        self.assertEqual(f['today_4xx'], 2, '429 + 403 应计 2 次 4xx')

    def test_successful_requests_are_not_counted_as_failures(self) -> None:
        now = int(time.time())
        _insert_log(now, self.kid, 200, pt=10, ct=5, credit=0.1)
        db.bump_usage(self.kid, 'deepseek-v4.1-flash', 10, 5, 0.1, realm='cn')
        f = self._summary()['failures']
        self.assertEqual((f['today_4xx'], f['today_5xx']), (0, 0))

    def test_keyless_401_is_excluded(self) -> None:
        """没带密钥的 401 是扫描器/配置噪声，不算失败（否则信号被淹没）。"""
        now = int(time.time())
        _insert_log(now, None, 401)
        _insert_log(now, self.kid, 403)
        f = self._summary()['failures']
        self.assertEqual(f['today_4xx'], 1, '只有带密钥的那条算失败')

    def test_usage_table_still_excludes_failures(self) -> None:
        """反证用：确认「用量表里确实没有失败」这个前提仍然成立。

        若哪天 `bump_usage` 改成对失败也计数，本文件的前提就变了——
        这条会失败并提醒复核（失败数就不必单独取日志了）。
        """
        now = int(time.time())
        _insert_log(now, self.kid, 503)
        _insert_log(now, self.kid, 200, pt=10, ct=5, credit=0.1)
        db.bump_usage(self.kid, 'deepseek-v4.1-flash', 10, 5, 0.1, realm='cn')
        row = db.query_one('SELECT COALESCE(SUM(requests),0) AS r FROM usage_daily')
        self.assertEqual(int(row['r']), 1, '前提变了：用量表现在会累计失败请求')

    # ── 2. 按天聚合 ──────────────────────────────────────────
    def test_daily_carries_failed_count(self) -> None:
        now = int(time.time())
        _insert_log(now, self.kid, 200, pt=10, ct=5, credit=0.1)
        db.bump_usage(self.kid, 'deepseek-v4.1-flash', 10, 5, 0.1, realm='cn')
        _insert_log(now, self.kid, 503)
        _insert_log(now, self.kid, 502)
        rows = self._daily()
        today = time.strftime('%Y-%m-%d')
        row = next((r for r in rows if r['day'] == today), None)
        self.assertIsNotNone(row, '今天没有出现在 daily 里')
        self.assertEqual(row['failed'], 2)
        self.assertEqual(row['requests'], 1, '请求数仍是成功量（口径不变）')

    def test_failure_only_day_appears(self) -> None:
        """**只有失败、没有成功**的日子也必须出现。

        这正是线上那次凌晨中断的形态：31 次失败、0 次成功。若按用量表出行，
        那天会**整条缺失**（用量表里没有它的任何记录），趋势图上等于那天不存在
        ——比画成 0 更糟，用户完全看不到出过事。
        """
        day3 = int(time.time()) - 3 * 86400
        for i in range(31):
            _insert_log(day3 + i, self.kid, 503)
        rows = self._daily()
        target = time.strftime('%Y-%m-%d', time.localtime(day3))
        hit = next((r for r in rows if r['day'] == target), None)
        self.assertIsNotNone(hit, f'{target}（纯失败日）没有出现在 daily 里')
        self.assertEqual(hit['failed'], 31)
        self.assertEqual(hit['requests'], 0)
        # 同时确认那一天确实不在用量表里（说明这条覆盖了真实盲区）
        used = {r['day'] for r in db.query('SELECT DISTINCT day FROM usage_daily')}
        self.assertNotIn(target, used, '前提变了：用量表里居然有纯失败日')

    def test_daily_sorted_after_merging(self) -> None:
        """合并两种来源后仍要按日期升序（前端图表直接按序画）。"""
        base = int(time.time())
        for back in (5, 3, 1):
            _insert_log(base - back * 86400, self.kid, 503)
        _insert_log(base, self.kid, 200, pt=1, ct=1, credit=0.01)
        db.bump_usage(self.kid, 'deepseek-v4.1-flash', 1, 1, 0.01, realm='cn')
        days = [r['day'] for r in self._daily()]
        self.assertEqual(days, sorted(days), f'未按日期升序：{days}')

    # ── 3. 版本过滤 ─────────────────────────────────────────
    def test_realm_filter_applies(self) -> None:
        now = int(time.time())
        _insert_log(now, self.kid, 503, realm='cn')
        _insert_log(now, self.kid, 503, realm='global')
        self.assertEqual(self._summary(realm='cn')['failures']['today_5xx'], 1)
        self.assertEqual(self._summary(realm='global')['failures']['today_5xx'], 1)

    def test_historical_null_realm_counts_as_cn(self) -> None:
        """历史日志的 realm 是 NULL（该列后加），应按 cn 归类（与用量表同口径）。"""
        now = int(time.time())
        db.execute(
            'INSERT INTO request_logs(ts,key_id,ip,model,mapped_model,status,prompt_tokens,'
            'completion_tokens,latency_ms,ua,error,stream,credit,realm) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
            (now, self.kid, '1.2.3.4', 'm', 'm', 503, 0, 0, 100, 'UA', None, 1, None),
        )
        self.assertEqual(self._summary(realm='cn')['failures']['today_5xx'], 1)
        self.assertEqual(self._summary(realm='global')['failures']['today_5xx'], 0)


class FailureCountNeverBreaksSummaryTest(unittest.TestCase):
    """失败数是旁路统计，算不出来也不能让总览接口挂掉。"""

    def test_returns_zeros_on_error(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        orig = config.DB_PATH
        config.DB_PATH = Path(tmp.name) / 'x.db'
        db._conn = None
        try:
            db.connect()
            # 把表删掉，制造一个必然报错的查询环境
            db.execute('DROP TABLE request_logs')
            f = stats._failures()
            self.assertEqual(f['today_5xx'], 0)
            self.assertEqual(f['week_4xx'], 0)
            self.assertEqual(stats._daily_failures(30), {})
        finally:
            try:
                if db._conn is not None:
                    db._conn.close()
            except Exception:  # noqa: BLE001
                pass
            db._conn = None
            config.DB_PATH = orig
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
