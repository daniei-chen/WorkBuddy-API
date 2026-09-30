"""更新状态判定的回归测试。

背景：管理端更新最后一步是 `systemctl restart`，而 systemd 默认
KillMode=control-group 会把更新进程（连同一个 cgroup 里的所有进程）一起终止。
旧逻辑一看到「标记运行中但进程没了」就判为「异常中断」，
于是更新明明成功、界面却显示「更新未完成」。

这里覆盖两条判定证据：
  1. 状态里记了目标版本，且已部署版本等于它
  2. 日志里出现「管理端已更新到 X，重启服务以生效」且 X 等于已部署版本

运行：python -m unittest discover -s server/tests -t . -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import updater  # noqa: E402


class UpdateLanded(unittest.TestCase):
    def setUp(self) -> None:
        self._orig = updater.current_version

    def tearDown(self) -> None:
        updater.current_version = self._orig

    def _at(self, version: str) -> None:
        updater.current_version = lambda: version  # type: ignore[assignment]

    def test_target_version_matches(self) -> None:
        self._at('v1.0.5')
        self.assertTrue(updater._update_landed({'target_version': '1.0.5', 'logs': []}))

    def test_target_version_mismatch(self) -> None:
        self._at('v1.0.4')
        self.assertFalse(updater._update_landed({'target_version': '1.0.5', 'logs': []}))

    def test_legacy_worker_without_target_version_uses_log(self) -> None:
        """旧版脚本不写 target_version，靠日志行兜底（过渡兼容）。"""
        self._at('v1.0.5')
        status = {
            'target_version': '',
            'logs': [
                {'text': '== 更新管理端 =='},
                {'text': '管理端已更新到 v1.0.5，重启服务以生效'},
                {'text': '$ systemctl restart workbuddy-web'},
            ],
        }
        self.assertTrue(updater._update_landed(status))

    def test_log_version_does_not_match_current(self) -> None:
        """日志说更新到了 X，但磁盘上还是旧版本 → 仍算失败。"""
        self._at('v1.0.4')
        status = {
            'logs': [{'text': '管理端已更新到 v1.0.5，重启服务以生效'}],
        }
        self.assertFalse(updater._update_landed(status))

    def test_no_evidence_is_failure(self) -> None:
        self._at('v1.0.4')
        self.assertFalse(updater._update_landed({'logs': []}))


class VersionCompare(unittest.TestCase):
    """版本比较必须是「严格更新」，否则回滚场景会冒出降级提示。

    真实事故：更新到 v1.0.5 后，6 小时缓存里还存着 v1.0.4，
    旧逻辑用「不相等」判断，于是界面显示「管理端：v1.0.5 → v1.0.4」。
    """

    def test_newer_detected(self) -> None:
        for remote, current in [
            ('v1.0.5', 'v1.0.4'),
            ('1.0.10', '1.0.9'),   # 数字段按数值比，不能按字符串
            ('v1.1.0', 'v1.0.99'),
            ('v2.0.0', 'v1.99.99'),
            ('v1.0.1', 'v1.0'),
        ]:
            self.assertTrue(updater._version_newer(remote, current), f'{remote} > {current}')

    def test_equal_is_not_an_update(self) -> None:
        for a, b in [('v1.0.5', '1.0.5'), ('1.0.5', 'v1.0.5'), ('v1.0', 'v1.0.0')]:
            self.assertFalse(updater._version_newer(a, b), f'{a} == {b}')

    def test_older_is_not_an_update(self) -> None:
        """降级（当前版本领先于远端）不能提示更新——这就是本次事故。"""
        for remote, current in [
            ('v1.0.4', 'v1.0.5'),
            ('v1.0.9', 'v1.0.10'),
            ('v1.0', 'v1.1'),
        ]:
            self.assertFalse(updater._version_newer(remote, current), f'{remote} < {current}')

    def test_unparseable_never_claims_update(self) -> None:
        self.assertFalse(updater._version_newer('latest', 'v1.0.5'))
        self.assertFalse(updater._version_newer('v1.0.5', 'unknown'))
        self.assertFalse(updater._version_newer('', 'v1.0.5'))

    def test_prerelease_suffix_ignored(self) -> None:
        self.assertTrue(updater._version_newer('v1.0.6-rc1', 'v1.0.5'))
        self.assertFalse(updater._version_newer('v1.0.5-rc1', 'v1.0.5'))


class UpstreamChanges(unittest.TestCase):
    """上游「领先多少个提交 + 逐条说明」的解析。

    动机：原先只显示最新一条提交，看不出这批更新累积了几处改动，
    也分不出是功能还是修复（用户明确问过「上游更新了什么」）。
    """

    def setUp(self) -> None:
        self._orig = updater._gh_get

    def tearDown(self) -> None:
        updater._gh_get = self._orig

    def _fake(self, payload, raises=None):
        def _get(url, timeout=15):
            if raises:
                raise raises
            return payload
        updater._gh_get = _get  # type: ignore[assignment]

    def _compare(self, commits, ahead=None, total=None):
        return {
            'ahead_by': ahead if ahead is not None else len(commits),
            'total_commits': total if total is not None else len(commits),
            'commits': commits,
        }

    @staticmethod
    def _c(sha, msg, date='2026-09-13T08:00:00Z'):
        return {'sha': sha, 'commit': {'message': msg, 'committer': {'date': date}}}

    def test_parses_ahead_and_subjects(self) -> None:
        self._fake(self._compare([
            self._c('a' * 40, 'fix: 修复一' + chr(10) + chr(10) + '详细说明'),
            self._c('b' * 40, 'feat: 新功能'),
        ]))
        r = updater._fetch_upstream_changes('o/r', 'x' * 40, 'y' * 40)
        self.assertEqual(r['ahead'], 2)
        self.assertEqual(len(r['changes']), 2)
        # 最新的在前
        self.assertTrue(r['changes'][0]['sha'].startswith('b'))
        # 只取首行
        self.assertEqual(r['changes'][1]['subject'], 'fix: 修复一')

    def test_changes_capped(self) -> None:
        commits = [self._c(f'{i:040x}', f'c{i}') for i in range(30)]
        self._fake(self._compare(commits, ahead=30, total=30))
        r = updater._fetch_upstream_changes('o/r', 'x' * 40, 'y' * 40)
        self.assertEqual(len(r['changes']), updater._CHANGES_LIMIT)
        self.assertTrue(r['truncated'], '超出上限应标记截断')

    def test_same_sha_short_circuits(self) -> None:
        """同一版本不必请求 compare。"""
        self._fake({'commits': []})
        r = updater._fetch_upstream_changes('o/r', 'a' * 40, 'a' * 40)
        self.assertEqual(r['ahead'], 0)
        self.assertEqual(r['changes'], [])

    def test_network_error_degrades_silently(self) -> None:
        """拿不到变更列表不能影响版本检测本身。"""
        self._fake(None, raises=RuntimeError('boom'))
        r = updater._fetch_upstream_changes('o/r', 'x' * 40, 'y' * 40)
        self.assertEqual(r['ahead'], 0)
        self.assertEqual(r['changes'], [])

    def test_malformed_payload_is_safe(self) -> None:
        for payload in ({}, {'commits': None}, {'commits': 'x'}, []):
            self._fake(payload)
            r = updater._fetch_upstream_changes('o/r', 'x' * 40, 'y' * 40)
            self.assertIsInstance(r['changes'], list)

    def test_missing_sha_returns_empty(self) -> None:
        self._fake({'commits': []})
        self.assertEqual(updater._fetch_upstream_changes('o/r', '', 'y' * 40)['ahead'], 0)


if __name__ == '__main__':
    unittest.main()
