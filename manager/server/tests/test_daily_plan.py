"""每日任务计划（签到 / 领奖 / 做任务）的调度与配置。

需求：把管理端的「一键签到 / 一键领奖 / 做任务」做成每天到点的计划任务。
这里钉住几件事：

  1. 配置存 db（`daily_tasks_plan`），脏值要能收敛；**三个任务默认全关**
     （full 会伪造活跃上报，绝不能默认开）。
  2. `set_plan` 校验类型/范围，且**保留 `last_run`**（改时刻不该导致当天重跑）。
  3. 到点判定：未启用 / 整点没对上 / **这一小时已跑过** → 不跑。
  4. 分发：checkin → `accounts.checkin_all`；claim/full → `taskrun.start_async`。
  5. 手动「立即执行」也记「本小时已跑」，避免刚跑完到点又跑一次。
"""
from __future__ import annotations

import asyncio
import datetime
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import accounts as accounts_router  # noqa: E402
from server.services import dailyplan  # noqa: E402


def _ts(y: int, m: int, d: int, h: int) -> int:
    return int(datetime.datetime(y, m, d, h).timestamp())


class _Store:
    def __init__(self, initial: dict | None = None) -> None:
        self.store: dict = dict(initial or {})

    def get_setting(self, key: str, default=None):
        return self.store.get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.store[key] = value


def _patch_db(initial: dict | None = None):
    store = _Store(initial)
    return mock.patch.multiple(dailyplan.db, get_setting=store.get_setting,
                               set_setting=store.set_setting), store


def _body(checkin=(False, 9), claim=(False, 10), full=(False, 10),
          compensation=(False, 10)) -> dict:
    # 四个任务默认全关（compensation 也一样：日常自动由上游活跃上报负责，
    # 面板这条主打「立即执行」手动补一次 —— 见 dailyplan._DEFAULTS 的注释）。
    return {
        'checkin': {'enabled': checkin[0], 'hour': checkin[1]},
        'claim': {'enabled': claim[0], 'hour': claim[1]},
        'full': {'enabled': full[0], 'hour': full[1]},
        'compensation': {'enabled': compensation[0], 'hour': compensation[1]},
    }


class PlanConfigTest(unittest.TestCase):
    def test_defaults(self) -> None:
        """四个任务默认全关——尤其 full（伪造活跃上报）绝不能默认开。

        compensation（领取活动补偿）也默认关，尽管它本身很安全：**上游的活跃上报
        每天已经在领同一笔**，面板这条是「马上领一次」的手动兜底（公告带截止日期），
        默认开只会让每个账号每天多一次注定「无可领」的写请求。
        """
        patcher, _ = _patch_db()
        with patcher:
            plan = dailyplan.get_plan()
        for task in dailyplan.TASKS:
            self.assertFalse(plan[task]['enabled'], f'{task} 不该默认开')
        self.assertEqual(plan['claim']['hour'], 10)
        self.assertEqual(plan['compensation']['hour'], 10)

    def test_corrupt_values_are_normalised(self) -> None:
        patcher, _ = _patch_db({'daily_tasks_plan': {
            'checkin': {'enabled': 'yes', 'hour': 99},
            'claim': 'not-a-dict',
            'full': {'enabled': True, 'hour': 25},
            'compensation': {'enabled': 'yes', 'hour': -1},
        }})
        with patcher:
            plan = dailyplan.get_plan()
        self.assertFalse(plan['compensation']['enabled'], '非法值应回落到默认（关）')
        self.assertEqual(plan['compensation']['hour'], 10)
        self.assertFalse(plan['checkin']['enabled'])
        self.assertEqual(plan['checkin']['hour'], 9)
        self.assertEqual(plan['claim']['hour'], 10)
        self.assertTrue(plan['full']['enabled'])
        self.assertEqual(plan['full']['hour'], 10, '越界小时应用默认值兜底')

    def test_set_plan_validates_types(self) -> None:
        patcher, _ = _patch_db()
        with patcher:
            for bad in ({'checkin': {'enabled': 1, 'hour': 9},
                         'claim': {'enabled': False, 'hour': 10},
                         'full': {'enabled': False, 'hour': 10},
                         'compensation': {'enabled': False, 'hour': 10}},
                        'nope'):
                with self.subTest(bad=bad), self.assertRaises(TypeError):
                    dailyplan.set_plan(bad)

    def test_set_plan_validates_range(self) -> None:
        patcher, _ = _patch_db()
        with patcher:
            with self.assertRaises(ValueError):
                dailyplan.set_plan(_body(checkin=(True, 24)))

    def test_set_plan_preserves_last_run(self) -> None:
        patcher, _ = _patch_db()
        with patcher:
            dailyplan.mark_ran('claim', now=_ts(2026, 10, 13, 10))
            plan = dailyplan.set_plan(_body(claim=(True, 11)))
        self.assertEqual(plan['claim']['last_run'], '2026-10-13-10')


class IsDueTest(unittest.TestCase):
    def _plan(self, **kw) -> dict:
        base = {'checkin': {'enabled': False, 'hour': 9, 'last_run': ''},
                'claim': {'enabled': True, 'hour': 10, 'last_run': ''},
                'full': {'enabled': False, 'hour': 10, 'last_run': ''}}
        base.update(kw)
        return base

    def test_disabled_never_due(self) -> None:
        self.assertFalse(dailyplan.is_due('checkin', self._plan(), now=_ts(2026, 10, 13, 9)))

    def test_only_at_configured_hour(self) -> None:
        p = self._plan()
        self.assertFalse(dailyplan.is_due('claim', p, now=_ts(2026, 10, 13, 9)))
        self.assertTrue(dailyplan.is_due('claim', p, now=_ts(2026, 10, 13, 10)))

    def test_same_hour_does_not_rerun(self) -> None:
        """同一小时已经跑过就不该再跑（巡检是每分钟一跳，否则会跑 60 次）。"""
        p = self._plan(claim={'enabled': True, 'hour': 10, 'last_run': '2026-10-13-10'})
        self.assertFalse(dailyplan.is_due('claim', p, now=_ts(2026, 10, 13, 10)))

    def test_next_day_runs_again(self) -> None:
        p = self._plan(claim={'enabled': True, 'hour': 10, 'last_run': '2026-10-13-10'})
        self.assertTrue(dailyplan.is_due('claim', p, now=_ts(2026, 10, 14, 10)))


class RunTaskTest(unittest.TestCase):
    def test_checkin_dispatches_to_checkin_all(self) -> None:
        async def fake(upstream_id=None, user=None):
            return {'total': 3, 'succeeded': 2, 'already': 1}
        with mock.patch.object(accounts_router, 'checkin_all', fake):
            ok, msg = asyncio.run(dailyplan.run_task('checkin'))
        self.assertTrue(ok)
        self.assertIn('2/3', msg)

    def test_claim_and_full_dispatch_to_taskrun(self) -> None:
        calls: list = []

        async def fake_start(mode, target='ALL'):
            calls.append((mode, target))
            return True, 'started'

        with mock.patch.object(dailyplan.taskrun, 'start_async', fake_start):
            asyncio.run(dailyplan.run_task('claim'))
            asyncio.run(dailyplan.run_task('full'))
        self.assertEqual(calls, [('claim', 'ALL'), ('full', 'ALL')])

    def test_unknown_task_rejected(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(dailyplan.run_task('nope'))


class RunNowEndpointTest(unittest.TestCase):
    """「立即执行」：跑对应任务，并把本小时标记为已跑。"""

    def test_run_now_marks_ran(self) -> None:
        seen: dict = {}

        async def fake_run(task):
            seen['task'] = task
            return True, 'ok'

        def fake_mark(task, now=None):
            seen['marked'] = task
            return {'checkin': {'enabled': True, 'hour': 9, 'last_run': 'x'},
                    'claim': {}, 'full': {}}

        with mock.patch.object(accounts_router.dailyplan, 'run_task', fake_run), \
             mock.patch.object(accounts_router.dailyplan, 'mark_ran', fake_mark):
            out = asyncio.run(accounts_router.run_daily_task_now(
                body={'task': 'claim'}, user={'role': 'admin'}))

        self.assertEqual(seen.get('task'), 'claim')
        self.assertEqual(seen.get('marked'), 'claim')
        self.assertTrue(out['ok'])
        self.assertEqual(out['task'], 'claim')

    def test_bad_task_rejected(self) -> None:
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(accounts_router.run_daily_task_now(
                body={'task': 'nope'}, user={'role': 'admin'}))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
