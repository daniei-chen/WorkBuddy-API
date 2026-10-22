"""上游任务日志解析的回归测试。

这些日志行来自真实上游源码（travel.go / scheduler.go）的 log.Printf，
容器里带 docker --timestamps 前缀。解析必须准确提取类型、账号与积分，
否则界面上要么看不到收益，要么把跳过当成功。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import tasklog  # noqa: E402

DOCKER = '2026-09-11T17:43:44.123456789Z '


class ParseTaskLines(unittest.TestCase):
    def test_travel_claim_reward(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}2026/09/11 17:43:44 travel 89374120: claim ok record=12 reward=100'
        )
        assert ev is not None
        self.assertEqual(ev['kind'], 'travel')
        self.assertEqual(ev['uid'], '89374120')
        self.assertEqual(ev['credits'], 100)
        self.assertEqual(ev['level'], 'credit')

    def test_travel_adopt_reward(self) -> None:
        ev = tasklog.parse_line(f'{DOCKER}travel 89374120: adopt ok (+300 credits)')
        assert ev is not None
        self.assertEqual(ev['kind'], 'travel')
        self.assertEqual(ev['credits'], 300)
        self.assertEqual(ev['level'], 'credit')

    def test_travel_depart_is_not_a_credit(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}2026/09/11 09:00:00 travel 89374120: depart ok location=7'
        )
        assert ev is not None
        self.assertEqual(ev['credits'], 0)
        self.assertEqual(ev['level'], 'ok')

    def test_travel_skip_is_info_not_error(self) -> None:
        for line in (
            'travel 89374120: skip (daily limit reached)',
            'travel 89374120: skip (traveling record=3)',
        ):
            ev = tasklog.parse_line(f'{DOCKER}{line}')
            assert ev is not None
            self.assertEqual(ev['level'], 'info', line)
            self.assertEqual(ev['credits'], 0)

    def test_activity_streak_ok(self) -> None:
        ev = tasklog.parse_line(f'{DOCKER}2026/09/11 10:00:00 activity 89374120: streak days=3')
        assert ev is not None
        self.assertEqual(ev['kind'], 'activity')
        self.assertEqual(ev['level'], 'ok')
        self.assertIn('streak', ev['message'])

    def test_activity_silent_drop_is_warn(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}activity 89374120: report OK but streak.days=0 (silent drop?)'
        )
        assert ev is not None
        self.assertEqual(ev['level'], 'warn')

    def test_failure_lines_are_errors(self) -> None:
        for line in (
            '2026/09/11 22:00:00 travel 89374120: status: context deadline exceeded',
            'checkin 89374120: rpc error: code = DeadlineExceeded',
            'keepalive 89374120: 连续 3 次 12153 session dead — 禁用',
            'user-resource 89374120: unexpected end of JSON input',
        ):
            ev = tasklog.parse_line(f'{DOCKER}{line}')
            assert ev is not None, line
            self.assertEqual(ev['level'], 'error', line)

    # ── 上游 2026-09-17 起的日志格式变化：账号标识变成 `昵称(uid8)` ────
    #
    # 上游 `logfmt.Label()`（internal/logfmt/logfmt.go:60）把约 30 处调度日志的
    # 账号标识从纯 uid8 改成 `昵称(uid8)`，理由是排障时人眼没法从 uid8 认出是哪个号。
    #
    # 这一组测试是**防静默失效**的：解析器原先只认 [0-9A-Za-z_-]，遇到含中文与
    # 括号的标签会直接不匹配——不报错、不崩溃，只是「任务记录」里账号维度的行
    # 全部消失。其中最要命的是 travel 的 `claim ok … reward=…`，它是唯一能拿到
    # 旅行积分的日志源，丢了它积分收益会恒显示 0（把「赚到了」显示成「没赚」）。
    def test_nickname_label_form(self) -> None:
        """`昵称(uid8)` 形态要能解析，且 uid 归一化回 uid8。"""
        ev = tasklog.parse_line(f'{DOCKER}checkin 猫猫(9b212d8c): 今日已签到')
        assert ev is not None
        self.assertEqual(ev['kind'], 'checkin')
        self.assertEqual(ev['uid'], '9b212d8c',
                         'uid 必须归一化回 uid8 —— 否则同一账号会按昵称分裂成多个')

    def test_nickname_label_keeps_credits(self) -> None:
        """旅行领奖的积分不能因为标签变形而丢失（这是唯一的积分来源）。"""
        ev = tasklog.parse_line(
            f'{DOCKER}travel 猫猫(9b212d8c): claim ok record=12 reward=100'
        )
        assert ev is not None
        self.assertEqual(ev['uid'], '9b212d8c')
        self.assertEqual(ev['credits'], 100)
        self.assertEqual(ev['level'], 'credit')

    def test_nickname_label_stage_line(self) -> None:
        """阶段行（标签与阶段名之间是空格）同样要认。"""
        ev = tasklog.parse_line(f'{DOCKER}checkin 猫猫(9b212d8c) refresh: invalid_grant')
        assert ev is not None
        self.assertEqual(ev['uid'], '9b212d8c')
        self.assertIn('refresh', ev['message'])

    def test_nickname_label_with_warn_prefix(self) -> None:
        ev = tasklog.parse_line(f'{DOCKER}WARN: activity 猫猫(9b212d8c): streak days=0')
        assert ev is not None
        self.assertEqual(ev['uid'], '9b212d8c')
        self.assertEqual(ev['level'], 'warn')

    def test_nickname_containing_parens_and_spaces(self) -> None:
        """昵称是自由文本：可能自带括号、空格、英文。

        取最外层括号 + 要求括号内是合法 uid 形态，才能正确切出 uid8。
        """
        for nick in ('猫猫(小)', 'My Cat 01', 'a(b)(c)', '小黑·测试'):
            with self.subTest(nick=nick):
                ev = tasklog.parse_line(f'{DOCKER}checkin {nick}(9b212d8c): 今日已签到')
                assert ev is not None, f'昵称 {nick!r} 的行被丢弃了'
                self.assertEqual(ev['uid'], '9b212d8c')

    def test_label_does_not_swallow_reserved_words(self) -> None:
        """放宽账号标识的匹配后，done / skipped 这类不能又被当成账号。

        这是改造的**主要风险**：`_LABEL_TOKEN` 放宽成「任意文本」，若不经过
        `_normalize_uid` 的保留字过滤，`checkin done: total=…` 会凭空多出一个
        叫 done 的账号，界面上就出现不存在的号。
        """
        for line in ('checkin done: total=3 ok=1 fail=0 skipped=0',
                     'scheduled checkin skipped: not in window',
                     'scheduled activity skipped: window'):
            with self.subTest(line=line):
                ev = tasklog.parse_line(f'{DOCKER}{line}')
                uid = (ev or {}).get('uid', '')
                self.assertEqual(uid, '', f'不该解析出账号: {line} → uid={uid!r}')

    def test_uid_truncated_to_8_chars(self) -> None:
        """上游把日志里的 uid 截成前 8 位。"""
        ev = tasklog.parse_line(
            f'{DOCKER}travel 9b212d8c: claim ok record=12 reward=100'
        )
        assert ev is not None
        self.assertEqual(ev['uid'], '9b212d8c')
        self.assertEqual(ev['credits'], 100)

    def test_warn_prefix_captured(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}WARN: activity 9b212d8c: report OK but streak.days=0 (silent drop?)'
        )
        assert ev is not None
        self.assertEqual(ev['kind'], 'activity')
        self.assertEqual(ev['uid'], '9b212d8c')
        self.assertEqual(ev['level'], 'warn')

    def test_err_prefix_captured(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}ERR: checkin 9b212d8c: context deadline exceeded'
        )
        assert ev is not None
        self.assertEqual(ev['level'], 'error')

    def test_activity_burst_lines(self) -> None:
        """活跃上报改 5 连发：report N/M ok / report N/M: <err>。"""
        ok = tasklog.parse_line(f'{DOCKER}activity 9b212d8c: report 1/5 ok')
        assert ok is not None
        self.assertEqual(ok['level'], 'ok')
        self.assertEqual(ok['kind'], 'activity')

        bad = tasklog.parse_line(f'{DOCKER}activity 9b212d8c: report 3/5: timeout')
        assert bad is not None
        self.assertEqual(bad['level'], 'error')

    def test_new_failure_wording_is_error(self) -> None:
        """WARN:/ERR: 之外，未加前缀的失败行仍应判为 error。"""
        for line in (
            'activity 9b212d8c: report 2/5: connection reset',
            'activity 9b212d8c: buddy-info: timeout',
        ):
            ev = tasklog.parse_line(f'{DOCKER}{line}')
            assert ev is not None, line
            self.assertEqual(ev['level'], 'error', line)

    def test_report_burst_translated(self) -> None:
        self.assertEqual(
            tasklog.translate_message('report 1/5 ok'), '活跃上报成功（第 1/5 条）'
        )
        self.assertEqual(
            tasklog.translate_message('report 3/5: timeout'), '活跃上报失败（第 3/5 条）'
        )

    # ── 上游 2026-09-12 签到健壮性（PR #48）带来的新日志形态 ──────
    def test_already_checkin_is_success_not_error(self) -> None:
        """「今天已签到」是幂等成功，不能显示成失败。

        上游改版后成功/已签到也打日志，且腾讯把它当业务错误返回，
        不特判就会在界面上变成一整片红色「签到失败」。
        """
        for text in ('今日已签到', '今天已签到，请勿重复签到', 'already checked in'):
            ev = tasklog.parse_line(f'{DOCKER}checkin 9b212d8c: {text}')
            assert ev is not None, text
            self.assertEqual(ev['level'], 'ok', text)

    def test_stage_failure_lines_capture_uid(self) -> None:
        """`checkin <uid> refresh|save: <err>` 是真实失败，要能记下账号。"""
        ev = tasklog.parse_line(f'{DOCKER}checkin 9b212d8c refresh: token invalid')
        assert ev is not None
        self.assertEqual(ev['uid'], '9b212d8c')
        self.assertEqual(ev['kind'], 'checkin')
        self.assertEqual(ev['level'], 'error')
        self.assertIn('refresh', ev['message'])

        ev2 = tasklog.parse_line(f'{DOCKER}checkin 9b212d8c save: disk full')
        assert ev2 is not None
        self.assertEqual(ev2['uid'], '9b212d8c')
        self.assertEqual(ev2['level'], 'error')

    def test_done_summary_is_not_an_account(self) -> None:
        """`checkin done: total=.. ok=..` 是每轮汇总，不得把 done 当账号。"""
        ev = tasklog.parse_line(
            f'{DOCKER}checkin done: total=3 ok=1 already=1 fail=1 skipped=0'
        )
        assert ev is not None
        self.assertEqual(ev['uid'], '', '汇总行不应带账号 uid')
        self.assertNotEqual(ev['uid'], 'done')
        self.assertEqual(ev['kind'], 'checkin')
        self.assertEqual(ev['level'], 'warn', '有失败时汇总应为 warn')
        self.assertIn('共 3 个', ev['message'])

    def test_done_summary_all_ok(self) -> None:
        ev = tasklog.parse_line(f'{DOCKER}checkin done: total=2 ok=2 already=0 fail=0 skipped=0')
        assert ev is not None
        self.assertEqual(ev['level'], 'ok')

    def test_scheduled_skip_is_not_an_account(self) -> None:
        ev = tasklog.parse_line(f'{DOCKER}scheduled checkin skipped: outside window')
        assert ev is not None
        self.assertEqual(ev['uid'], '')
        self.assertEqual(ev['level'], 'info')
        self.assertIn('未执行', ev['message'])

    def test_disabled_account_is_error_even_with_warn_prefix(self) -> None:
        """账号被禁用是严重结果，不该因为上游只标 WARN 而降级。"""
        ev = tasklog.parse_line(
            f'{DOCKER}WARN: checkin 9b212d8c: 连续 3 次 12153 session dead — 禁用'
        )
        assert ev is not None
        self.assertEqual(ev['level'], 'error')
        self.assertIn('禁用', ev['message'])

    def test_reserved_tokens_are_not_uids(self) -> None:
        """阶段/汇总行里的英文单词不得被当成账号 uid。"""
        for word in ('done', 'skipped', 'refresh', 'save', 'total', 'ok', 'fail'):
            self.assertFalse(tasklog._is_uid(word), word)

    def test_real_uid_shapes_accepted(self) -> None:
        """真实 uid（8 位前缀或 uuid）必须被接受。

        特别包含「前缀全为 a-f 字母」的 uuid——不能因为要求含数字而丢掉它。
        """
        for ok in ('9b212d8c', '89374120', 'abcdefab',
                   '9b212d8c-f5f7-4ad6-aa20-1d576508c8c1'):
            self.assertTrue(tasklog._is_uid(ok), ok)

    def test_unrelated_lines_ignored(self) -> None:
        for line in (
            '',
            '   ',
            'listening on :7863',
            'INFO server started',
            '2026/09/11 17:00:00 some other module: hello',
        ):
            self.assertIsNone(tasklog.parse_line(line), repr(line))

    def test_timestamp_parsed_from_docker_prefix(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}travel 89374120: claim ok record=1 reward=50'
        )
        assert ev is not None
        # 2026-09-11T17:43:44Z
        self.assertEqual(ev['ts'], 1789148624)

    def test_timestamp_missing_still_parses(self) -> None:
        """没有 docker 前缀（直接跑脚本）时也应能解析，时间留 0 由上层兜底。"""
        ev = tasklog.parse_line('travel 89374120: claim ok record=1 reward=50')
        assert ev is not None
        self.assertEqual(ev['ts'], 0)
        self.assertEqual(ev['credits'], 50)

    def test_dedup_key_is_stable_and_unique(self) -> None:
        line = f'{DOCKER}travel 89374120: claim ok record=1 reward=50'
        a = tasklog.parse_line(line)
        b = tasklog.parse_line(line)
        assert a and b
        self.assertEqual(a['dedup_key'], b['dedup_key'])

        other = tasklog.parse_line(
            '2026-09-11T17:43:45.1Z travel 89374120: claim ok record=2 reward=50'
        )
        assert other is not None
        self.assertNotEqual(a['dedup_key'], other['dedup_key'])

    def test_parse_lines_mixed(self) -> None:
        lines = [
            'listening on :7863',
            f'{DOCKER}travel 89374120: claim ok record=1 reward=100',
            f'{DOCKER}activity 91203877: streak days=1',
            f'{DOCKER}travel 88110234: skip (daily limit reached)',
        ]
        events = tasklog.parse_lines(lines)
        self.assertEqual(len(events), 3)
        self.assertEqual([e['kind'] for e in events], ['travel', 'activity', 'travel'])

    # ── 结果文案中文化 ──────────────────────────────────
    def test_credit_messages_translated(self) -> None:
        self.assertEqual(
            tasklog.translate_message('claim ok record=12 reward=100'),
            '领奖成功：第 12 次行程，获得 100 积分',
        )
        self.assertEqual(
            tasklog.translate_message('adopt ok (+300 credits)'),
            '领养成功：获得 300 积分',
        )

    def test_skip_and_streak_translated(self) -> None:
        self.assertEqual(
            tasklog.translate_message('skip (daily limit reached)'),
            '跳过：今日次数已达上限',
        )
        self.assertEqual(tasklog.translate_message('streak days=3'), '连续登录 3 天')
        self.assertIn('静默丢弃', tasklog.translate_message(
            'report OK but streak.days=0 (silent drop?)'))

    def test_technical_error_phrases_translated(self) -> None:
        self.assertEqual(tasklog.translate_message('checkin ok code=0'), '签到成功')
        self.assertIn('请求超时', tasklog.translate_message('status: context deadline exceeded'))
        self.assertIn('JSON 解析失败', tasklog.translate_message('unexpected end of JSON input'))

    def test_own_chinese_ledger_message_untouched(self) -> None:
        msg = '余额 +100（1300 → 1400） · 黑天鹅'
        self.assertEqual(tasklog.translate_message(msg), msg)

    def test_unknown_message_falls_back_to_original(self) -> None:
        msg = 'some brand new upstream wording'
        self.assertEqual(tasklog.translate_message(msg), msg)

    def test_truncated_marker_preserved(self) -> None:
        out = tasklog.translate_message('adopt ok (+300 credits) ... （已截断）')
        self.assertEqual(out, '领养成功：获得 300 积分 …（已截断）')

    def test_strip_docker_ts(self) -> None:
        self.assertEqual(
            tasklog.strip_docker_ts(f'{DOCKER}travel 1: claim ok record=1 reward=100'),
            'travel 1: claim ok record=1 reward=100',
        )
        # 没有前缀时原样返回
        self.assertEqual(tasklog.strip_docker_ts('travel 1: hello'), 'travel 1: hello')


if __name__ == '__main__':
    unittest.main()

class GrowthRewardLogTest(unittest.TestCase):
    """连登奖励与抽奖的日志解析（上游 2026-09-16 commit 91418c5 新增）。

    这两类日志挂在 `activity` 这个 kind 下（与活跃上报同一个），因此解析器
    **不会**因为不认识的 kind 而整个漏掉——但结果文案是新形态：

      `activity <uid>: redeem tier=7d ok (+100 credit, +5 energy, +1 chances)`
      `activity <uid>: lottery drawn prize=50 credits (credit)`

    其中两处必须特判，否则界面上显示错误：
      * 领奖的积分收益若不解析 → 显示 0（把「赚到了 100」显示成「没赚」）；
      * 抽奖成功若不加判定 → **落到末尾的 error**（它没有 ` ok ` 字样），
        中奖反而显示成红色失败——比不显示更糟。
    """

    def test_redeem_credits_extracted(self) -> None:
        ev = tasklog.parse_line(
            f'{DOCKER}activity 89374120: redeem tier=7d ok (+100 credit, +5 energy, +1 chances)'
        )
        assert ev is not None
        self.assertEqual(ev['kind'], 'activity')
        self.assertEqual(ev['credits'], 100, '领奖积分没解析出来')
        self.assertEqual(ev['level'], 'credit')

    def test_redeem_other_tiers(self) -> None:
        for tier, credit in (('14d', 200), ('28d', 500)):
            ev = tasklog.parse_line(
                f'{DOCKER}activity 89374120: redeem tier={tier} ok (+{credit} credit, +9 energy, +3 chances)'
            )
            assert ev is not None
            self.assertEqual(ev['credits'], credit, tier)

    def test_redeem_energy_and_chances_are_not_credits(self) -> None:
        """只有 credit 段算积分：energy / chances 不是积分，不能混进收益。"""
        ev = tasklog.parse_line(
            f'{DOCKER}activity 89374120: redeem tier=7d ok (+0 credit, +50 energy, +99 chances)'
        )
        assert ev is not None
        self.assertEqual(ev['credits'], 0)

    def test_redeem_skip_is_not_error(self) -> None:
        """天数不足 / 已领过是正常态（上游 409/403），不该显示成失败。

        判成 `ok` 而不是 `info`：文案里的 `already claimed` 命中既有的
        「幂等成功」标记（与签到「今天已签到」同一处理）——已领过就是没事发生，
        不是「跳过了什么」。两者都不是失败，但 `ok` 与签到口径一致。
        """
        for line in (
            'redeem tier=7d skip (already claimed or days not enough)',
            'redeem tier=7d skip (days not enough)',
        ):
            ev = tasklog.parse_line(f'{DOCKER}activity 89374120: {line}')
            assert ev is not None
            self.assertNotEqual(ev['level'], 'error', line)
            self.assertEqual(ev['credits'], 0, line)

    def test_redeem_real_failure_is_error(self) -> None:
        ev = tasklog.parse_line(f'{DOCKER}activity 89374120: redeem tier=7d: boom')
        assert ev is not None
        self.assertEqual(ev['level'], 'error')

    def test_lottery_credit_prize_extracted(self) -> None:
        ev = tasklog.parse_line(f'{DOCKER}activity 89374120: lottery drawn prize=50 credits (credit)')
        assert ev is not None
        self.assertEqual(ev['credits'], 50, '抽奖中的积分奖品没解析出来')
        self.assertEqual(ev['level'], 'credit')

    def test_lottery_non_credit_prize_is_success(self) -> None:
        """非积分奖品（道具 / 谢谢参与）也是**成功**，不能判成失败。"""
        for prize in ('谢谢参与', '体验券', 'none'):
            ev = tasklog.parse_line(
                f'{DOCKER}activity 89374120: lottery drawn prize={prize} (none)'
            )
            assert ev is not None
            self.assertEqual(ev['level'], 'ok', prize)
            self.assertEqual(ev['credits'], 0, prize)

    def test_lottery_skip_is_info(self) -> None:
        for line in ('lottery skip (no chances)', 'lottery skip (no chances or disabled)'):
            ev = tasklog.parse_line(f'{DOCKER}activity 89374120: {line}')
            assert ev is not None
            self.assertEqual(ev['level'], 'info', line)

    def test_streak_line_is_ok(self) -> None:
        ev = tasklog.parse_line(f'{DOCKER}activity 89374120: streak days=8')
        assert ev is not None
        self.assertEqual(ev['level'], 'ok')
        self.assertEqual(ev['credits'], 0)

    def test_generic_gain_form(self) -> None:
        """通用收益形态 `<动作> ok (+N credit)` —— 上游一直在加新动作。

        写成通用规则（而不是逐个动作列举）的理由：这类日志上游**持续在加**
        （adopt / gift / compensation / …），每加一个漏一条的后果是**收益显示为 0**
        ——数字是错的但没有任何报错，最难发现。已经踩过三次。
        """
        for action in ('gift', 'compensation', 'adopt', 'claim', 'bonus', 'unknown-new-action'):
            ev = tasklog.parse_line(f'{DOCKER}activity 89374120: {action} ok (+80 credit)')
            assert ev is not None
            self.assertEqual(ev['credits'], 80, f'{action} 的收益没提取到')
            self.assertEqual(ev['level'], 'credit', action)

    def test_generic_rule_does_not_swallow_multi_item_parens(self) -> None:
        """**反证**：通用规则不能吞掉括号里有多项的形态。

        连登奖励是 `(+100 credit, +5 energy, +1 chances)` —— 通用规则要求
        括号里只有 credit 且紧跟右括号，所以它不会命中这条，仍由 redeem 规则
        处理。若通用规则写松了（例如匹配 `credit` 但不要求右括号），
        它会先命中并返回，把一条本应计入的事件变成只算 credit —— 虽然这里
        结果数值相同，但语义会漂移（例如未来 redeem 改成 credit 不在首位时
        会取错数字）。所以这里钉住「谁处理谁」。
        """
        ev = tasklog.parse_line(
            f'{DOCKER}activity 89374120: redeem tier=7d ok (+100 credit, +5 energy, +1 chances)')
        assert ev is not None
        self.assertEqual(ev['credits'], 100)
        self.assertEqual(ev['level'], 'credit')
        # 多档位也要对
        ev2 = tasklog.parse_line(
            f'{DOCKER}activity 89374120: redeem tier=28d ok (+500 credit, +9 energy, +3 chances)')
        assert ev2 is not None
        self.assertEqual(ev2['credits'], 500)

    def test_makeup_line(self) -> None:
        """补签卡保连登（上游 243c7f2）：成功无积分收益，失败仍算 error。"""
        ok = tasklog.parse_line(f'{DOCKER}activity 89374120: makeup ok 2026-09-15 (+streak kept)')
        assert ok is not None
        self.assertEqual(ok['level'], 'ok')
        self.assertEqual(ok['credits'], 0, '补签本身不产生积分收益')

        bad = tasklog.parse_line(f'{DOCKER}activity 89374120: makeup 2026-09-15: boom')
        assert bad is not None
        self.assertEqual(bad['level'], 'error')

    def test_zero_credit_still_counts_as_credit_level(self) -> None:
        """`(+0 credit)` 是「明确告知没有收益」，与「没提收益」不同。

        返回 level='credit' 让界面能区分「这次活动给了 0」与「这条日志与积分无关」。
        """
        ev = tasklog.parse_line(f'{DOCKER}checkin 89374120: ok (+0 credit)')
        assert ev is not None
        self.assertEqual(ev['credits'], 0)
        self.assertEqual(ev['level'], 'credit')

    def test_depart_without_credit_is_plain_ok(self) -> None:
        """没有收益括号的行不该被通用规则误判成 credit。"""
        ev = tasklog.parse_line(f'{DOCKER}travel 89374120: depart ok location=7')
        assert ev is not None
        self.assertEqual(ev['credits'], 0)
        self.assertEqual(ev['level'], 'ok')
        self.assertEqual(ev['credits'], 0)


class ScriptTaskKindsTest(unittest.TestCase):
    """第五、六类任务（开学季 / 夜猫）的日志解析。

    上游 2026-09-14 把这两类从宿主机 crontab 迁入内置调度器。它们不是
    「每账号一个 uid」的形态，而是整批跑一个脚本，日志只有成败两行：
        <kind>: ok (<script>)
        WARN: <kind> (<script>): <err>
    与既有四种形态（都要求 `<kind> <token>:`）不同，不加专门处理就会
    在「任务记录」里完全不可见——这是实测确认过的缺口。
    """

    def test_success_line(self) -> None:
        ev = tasklog.parse_line('school: ok (scripts/school_open_day_2026.py)')
        self.assertIsNotNone(ev)
        self.assertEqual(ev['kind'], 'school')
        self.assertEqual(ev['level'], 'ok')
        self.assertIn('成功', ev['message'])

    def test_failure_line(self) -> None:
        ev = tasklog.parse_line('WARN: school (scripts/school_open_day_2026.py): exit status 1')
        self.assertIsNotNone(ev)
        self.assertEqual(ev['kind'], 'school')
        self.assertEqual(ev['level'], 'warn')
        self.assertIn('exit status 1', ev['message'])

    def test_cat_both_forms(self) -> None:
        ok = tasklog.parse_line('cat: ok (scripts/task_runner.py)')
        self.assertEqual((ok['kind'], ok['level']), ('cat', 'ok'))
        bad = tasklog.parse_line('WARN: cat (scripts/task_runner.py): exit status 2')
        self.assertEqual((bad['kind'], bad['level']), ('cat', 'warn'))

    def test_timestamp_parsed(self) -> None:
        ev = tasklog.parse_line(
            '2026-09-14T12:00:01.123456789Z school: ok (scripts/x.py)')
        self.assertGreater(ev['ts'], 0, 'docker 时间戳应被解析')

    def test_labels_present(self) -> None:
        """两类任务要有中文名，否则筛选栏会显示英文 key。"""
        self.assertIn('school', tasklog.KIND_LABELS)
        self.assertIn('cat', tasklog.KIND_LABELS)

    def test_existing_forms_not_hijacked(self) -> None:
        """加了新分支后，原有的 uid 形态不能被新规则吞掉。"""
        ev = tasklog.parse_line('checkin 9b212d8c: 签到成功')
        self.assertEqual(ev['kind'], 'checkin')
        self.assertEqual(ev['uid'], '9b212d8c')
        # 汇总行仍走汇总分支（不是被当成脚本任务）
        ev2 = tasklog.parse_line('checkin done: total=3 ok=1 already=0 fail=0 skipped=0')
        self.assertEqual(ev2['kind'], 'checkin')
        self.assertIn('本轮签到完成', ev2['message'])


class ScheduleHoursValidationTest(unittest.TestCase):
    """新增的两类任务时刻必须能被保存（白名单外的键会被静默丢弃）。"""

    def test_school_and_cat_hours_in_whitelist(self) -> None:
        from server.services.wb2api import _HOURS_KEYS, _sanitize_section
        self.assertIn('school_hours', _HOURS_KEYS)
        self.assertIn('cat_hours', _HOURS_KEYS)
        out = _sanitize_section('schedule', {'school_hours': [12], 'cat_hours': [1, 2]})
        self.assertEqual(out['school_hours'], [12])
        self.assertEqual(out['cat_hours'], [1, 2])

    def test_out_of_range_rejected(self) -> None:
        from server.services.wb2api import _sanitize_section
        for bad in ({'school_hours': [24]}, {'cat_hours': [-1]}, {'school_hours': []}):
            with self.assertRaises(ValueError, msg=str(bad)):
                _sanitize_section('schedule', bad)


class ScriptStdoutNotInContainerLogTest(unittest.TestCase):
    """**记录一个容易误判的点**：脚本内部的 stdout 不会进容器日志。

    上游 `runScript`（internal/scheduler/school.go）执行子进程后**只记一行**
    成败摘要，不转发子进程的 stdout：

        log.Printf("%s: ok (%s)", name, cmdArgs[1])          # 成功
        log.Printf("WARN: %s (%s): %v", name, ...)           # 失败

    所以 `scripts/task_runner.py` 里那些 `print(f"[task_runner] ...")`（上游
    2026-09-16 一口气加了 40 多条，用于 first_buddy / 开学季 / 小程序任务链）
    **根本不会出现在容器日志里**，我们的任务记录页看不到它们 —— 这是**正常**的，
    不是解析缺口。

    为什么值得写成测试：只看脚本源码会以为「新增了大量日志形态，解析器要跟」，
    于是白做一遍适配（我在核对其 2026-09-16 的 9 个提交时就差点这么判）。
    真正进日志的仍是 `<kind>: ok (<script>)` 那两行，由 ScriptTaskKindsTest 覆盖。

    这里把两类形态都钉住：(a) 脚本级摘要解析正常；(b) `[task_runner]` 前缀行
    确实不被解析（如实反映「它不在日志流里」，而不是让解析器去兼容一个
    永远不会出现的形态）。
    """

    def test_script_summary_lines_still_parse(self) -> None:
        """真正会进日志的两行 —— 必须能解析（这是我们要保证的）。"""
        ok = tasklog.parse_line(f'{DOCKER}cat: ok (scripts/task_runner.py)')
        assert ok is not None
        self.assertEqual(ok['kind'], 'cat')
        self.assertEqual(ok['level'], 'ok')

        warn = tasklog.parse_line(f'{DOCKER}WARN: cat (scripts/task_runner.py): exit status 1')
        assert warn is not None
        self.assertEqual(warn['kind'], 'cat')
        self.assertEqual(warn['level'], 'warn')

    def test_task_runner_stdout_is_not_parsed(self) -> None:
        """脚本内部输出不在日志流里 —— 解析器**不该**为它做兼容。

        若将来上游改成转发子进程 stdout（那会是一次明确的行为变更），
        这个测试会失败，提醒我们那时才需要适配。
        """
        for line in (
            '[task_runner] ab12cd34 Sequential_Tasks_1: only_claim 跳过（未 completed）',
            '[task_runner] ab12cd34 first_buddy: buddy/first -> ok credit=+100 energy=+5',
            '[task_runner] ab12cd34 school: claim ok 已入账',
        ):
            self.assertIsNone(
                tasklog.parse_line(f'{DOCKER}{line}'),
                f'该形态不应出现在日志流里，却解析出了内容：{line}',
            )


if __name__ == '__main__':
    unittest.main()
