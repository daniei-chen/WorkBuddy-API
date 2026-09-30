"""积分缓存与余额变动流水的回归测试。

要点：
  1. 缓存命中要能被识别（返回 cached 标记与缓存时长），否则界面会把
     滞后的数字当成刚查到的实时值。
  2. 余额增加必须留下流水——签到 / 活跃上报这些渠道上游不打日志，
     只能靠比对余额来覆盖。
  3. 首次见到账号只建立基线，不能把历史余额误报成「刚获得」。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import credits, tencent  # noqa: E402

AUTH = {'uid': '89374120', 'access_token': 'tok'}


class CreditsCacheAndLedger(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'c.db'
        db._conn = None
        db.connect()
        credits.invalidate()
        db.set_setting(credits._SNAP_KEY, '{}')

        self._calls = 0
        self._orig_fetch = tencent.fetch_credits

        async def fake_fetch(auth: dict):
            self._calls += 1
            return True, self.value, 'ok', []

        self.value = 1000
        tencent.fetch_credits = fake_fetch  # type: ignore[assignment]

    def tearDown(self) -> None:
        tencent.fetch_credits = self._orig_fetch  # type: ignore[assignment]
        credits.invalidate()
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def _get(self, **kw):
        return asyncio.run(credits.get_credits(AUTH, **kw))

    # ── 缓存可见性 ──────────────────────────────────────
    def test_first_call_is_live(self) -> None:
        ok, value, msg, cached, age, _exp = self._get()
        self.assertTrue(ok)
        self.assertEqual(value, 1000)
        self.assertFalse(cached, '首次查询不应标记为缓存')
        self.assertIsNone(age)
        self.assertNotIn('缓存', msg)

    def test_second_call_reports_cache(self) -> None:
        self._get()
        ok, value, msg, cached, age, _exp = self._get()
        self.assertTrue(cached, 'TTL 内应命中缓存')
        self.assertEqual(value, 1000)
        self.assertIsNotNone(age)
        self.assertIn('缓存', msg)
        self.assertEqual(self._calls, 1, '命中缓存时不应重复请求腾讯')

    def test_force_bypasses_cache(self) -> None:
        self._get()
        ok, _, _, cached, age, _exp = self._get(force=True)
        self.assertFalse(cached)
        self.assertIsNone(age)
        self.assertEqual(self._calls, 2)

    # ── 余额变动流水 ────────────────────────────────────
    def test_first_balance_only_baselines(self) -> None:
        self._get()
        self.assertEqual(db.task_log_stats()['total'], 0, '首次只建基线，不记流水')

    def test_balance_increase_is_logged(self) -> None:
        self._get()                      # 基线 1000
        self.value = 1100
        self._get(force=True)
        logs = db.list_task_logs(limit=10, kind='credit')
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['credits'], 100)
        self.assertIn('1100', logs[0]['message'])
        self.assertEqual(db.task_log_stats()['total_credits'], 100)

    def test_balance_decrease_not_logged(self) -> None:
        """消耗不是「获取」，不应进流水。"""
        self._get()
        self.value = 900
        self._get(force=True)
        self.assertEqual(db.task_log_stats()['total'], 0)

    def test_balance_unchanged_not_logged(self) -> None:
        self._get()
        self._get(force=True)
        self.assertEqual(db.task_log_stats()['total'], 0)

    def test_multiple_gains_accumulate(self) -> None:
        """多次增加应各记一条，累计正确。"""
        self._get()                       # 1000
        self.value = 1100
        self._get(force=True)             # +100
        self.value = 1400
        self._get(force=True)             # +300
        logs = db.list_task_logs(limit=10, kind='credit')
        self.assertEqual(sorted(l['credits'] for l in logs), [100, 300])
        self.assertEqual(db.task_log_stats()['total_credits'], 400)

    def test_cached_call_does_not_double_log(self) -> None:
        self._get()
        self.value = 1200
        self._get(force=True)
        self._get()                       # 命中缓存
        self.assertEqual(db.task_log_stats()['total'], 1)

    def test_error_is_not_cached(self) -> None:
        async def failing(auth: dict):
            return False, None, 'network down', []

        tencent.fetch_credits = failing  # type: ignore[assignment]
        self._get()
        ok, _, _, cached, _, _exp = self._get()
        self.assertFalse(ok)
        self.assertFalse(cached, '失败结果不应被缓存')

    def test_snapshot_survives_separate_process_state(self) -> None:
        """快照存 settings 表：清掉内存缓存后仍能继续比对。"""
        self._get()
        credits.invalidate()
        self.value = 1050
        self._get()
        logs = db.list_task_logs(limit=10, kind='credit')
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['credits'], 50)


if __name__ == '__main__':
    unittest.main()
