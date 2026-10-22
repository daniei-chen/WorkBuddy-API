"""每密钥限流：**只有放行的请求才占用窗口额度**（issue #52）。

## 现象

客户端速率一旦超过阈值，放行量不会稳定在阈值上，而是「凑满一窗之后持续全拒、
永不恢复」——只要客户端还在以高于阈值的速率重试。原因是判定前先 `append()`：
被拒绝的请求同样计入窗口，于是窗口计数永远降不回阈值以下，限流器与客户端重试
构成正反馈。

## 这里钉住的语义

「滑动窗口内**放行**了多少次」：

  · 放行数上限 = `RATE_MAX_PER_MIN`（与改动前一致，不多放）；
  · 超出的请求被拒，且**不占额度** —— 所以客户端把速率降下来能立刻恢复；
  · 持续高于阈值时，放行量稳定在阈值（按窗口滑动持续放行），而不是锁死。

最后一条是这次的关键：它正是「限速」与「熔断」的分界。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway as G  # noqa: E402

KEY = {'id': 7}


class _Clock:
    """可推进的假时钟（只替换 gateway 模块里的 time）。"""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _burst(clock: _Clock, n: int, gap: float) -> tuple[int, int]:
    """在假时钟上连打 n 次请求，返回 (放行数, 拒绝数)。"""
    ok = rejected = 0
    for _ in range(n):
        limited, _count = G._rate_limited(KEY)
        if limited:
            rejected += 1
        else:
            ok += 1
        clock.advance(gap)
    return ok, rejected


class RateWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        G._rate.clear()
        self.clock = _Clock()
        self._patches = [
            mock.patch.object(G, 'time', self.clock),
            mock.patch.object(G, 'RATE_MAX_PER_MIN', 3),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(G._rate.clear)
        for p in reversed(self._patches):
            self.addCleanup(p.stop)

    def test_allows_up_to_limit_then_rejects(self) -> None:
        self.assertEqual([G._rate_limited(KEY)[0] for _ in range(3)],
                         [False, False, False])
        self.assertEqual([G._rate_limited(KEY)[0] for _ in range(3)],
                         [True, True, True])

    def test_rejected_requests_do_not_consume_quota(self) -> None:
        """被拒的请求不该让窗口计数继续上涨——否则永远出不来。"""
        for _ in range(3):
            G._rate_limited(KEY)
        counts = [G._rate_limited(KEY)[1] for _ in range(5)]
        self.assertEqual(counts, [3, 3, 3, 3, 3],
                         '被拒绝的请求也计入窗口了（issue #52 的根因）')

    def test_admissions_keep_flowing_while_client_hammers(self) -> None:
        """持续高于阈值重试时，放行量应按窗口滑动稳定保持——而不是锁死。

        这是与旧行为唯一有区别的地方：旧实现下这里只会放行 3 次，
        之后无论过多久都不再放行（客户端不停止重试就永远不恢复）。
        """
        ok, rejected = _burst(self.clock, 600, gap=0.5)   # 300 秒，窗口 60 秒
        self.assertEqual(rejected, 600 - ok)
        self.assertGreaterEqual(ok, 12,
                                f'300 秒里只放行了 {ok} 次 —— 限流器变成了熔断（issue #52）')
        self.assertLessEqual(ok, 5 * 3, '放行数超过了窗口容量的上限')

    def test_recovers_immediately_after_slowing_down(self) -> None:
        for _ in range(3):
            G._rate_limited(KEY)
        self.assertTrue(G._rate_limited(KEY)[0])
        self.clock.advance(G.RATE_WINDOW + 1)
        self.assertFalse(G._rate_limited(KEY)[0], '窗口过去后仍被拒')

    def test_window_is_per_key(self) -> None:
        for _ in range(3):
            G._rate_limited(KEY)
        self.assertFalse(G._rate_limited({'id': 8})[0], '限流串到了别的密钥上')

    def test_zero_disables_limit(self) -> None:
        with mock.patch.object(G, 'RATE_MAX_PER_MIN', 0):
            self.assertEqual([G._rate_limited(KEY)[0] for _ in range(10)],
                             [False] * 10)


if __name__ == '__main__':
    unittest.main()
