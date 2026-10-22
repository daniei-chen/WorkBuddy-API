import argparse
import sys
import unittest
from unittest import mock

import task_common as tc


class AccountSpacingTest(unittest.TestCase):
    def test_first_account_has_no_wait_and_later_accounts_use_random_range(self):
        spacing = tc.AccountSpacing(30, 120)
        rng = mock.Mock()
        rng.uniform.side_effect = [31.5, 119.0]
        with mock.patch.object(tc.random, 'SystemRandom', return_value=rng), \
             mock.patch.object(tc.time, 'sleep') as sleep:
            spacing.wait()
            sleep.assert_not_called()
            spacing.wait()
            spacing.wait()
        self.assertEqual(sleep.call_args_list, [mock.call(31.5), mock.call(119.0)])
        self.assertEqual(rng.uniform.call_args_list, [mock.call(30,120), mock.call(30,120)])

    def test_default_spacing_does_not_sleep_or_draw(self):
        with mock.patch.object(tc.random, 'SystemRandom') as rng, \
             mock.patch.object(tc.time, 'sleep') as sleep:
            spacing = tc.AccountSpacing()
            spacing.wait()
            spacing.wait()
        rng.assert_not_called()
        sleep.assert_not_called()

    def test_cli_rejects_bad_ranges_and_keeps_zero_defaults(self):
        parser = argparse.ArgumentParser()
        tc.add_account_spacing_arguments(parser)
        defaults = tc.account_spacing_from_args(parser.parse_args([]),parser)
        self.assertEqual((defaults.minimum, defaults.maximum),(0,0))
        for minimum,maximum in [(-1,120),(30,29),(0,601)]:
            with self.subTest(pair=(minimum,maximum)), self.assertRaises(ValueError):
                tc.AccountSpacing(minimum,maximum)

    def test_growth_task_batch_staggers_only_eligible_accounts(self):
        import task_runner as runner
        auths = {key: {'uid': key} for key in ('one','skip','two')}
        with mock.patch.object(sys, 'argv', ['task_runner.py', 'one','skip','two',
                '--yes','--only','black_cat','--account-delay-min','30','--account-delay-max','120']), \
             mock.patch.object(tc, 'load_auth', side_effect=lambda key: auths[key]), \
             mock.patch.object(tc, 'auth_is_global', side_effect=lambda a: a['uid']=='skip'), \
             mock.patch.object(tc.random, 'SystemRandom') as rng, \
             mock.patch.object(tc.time, 'sleep') as sleep, \
             mock.patch.object(runner, 'process_account') as process:
            rng.return_value.uniform.return_value = 31.5
            runner.main()
        self.assertEqual([call.args[0]['uid'] for call in process.call_args_list], ['one','two'])
        sleep.assert_called_once_with(31.5)

    def test_school_task_batch_staggers_only_eligible_accounts(self):
        import school_open_day_2026 as school
        auths = {key: {'uid': key} for key in ('one','skip','two')}
        with mock.patch.object(sys, 'argv', ['school.py', 'one','skip','two',
                '--run','--yes','--account-delay-min','30','--account-delay-max','120']), \
             mock.patch.object(tc, 'load_auth', side_effect=lambda key: auths[key]), \
             mock.patch.object(tc, 'auth_is_global', side_effect=lambda a: a['uid']=='skip'), \
             mock.patch.object(tc.random, 'SystemRandom') as rng, \
             mock.patch.object(tc.time, 'sleep') as sleep, \
             mock.patch.object(school, 'run_account') as process:
            rng.return_value.uniform.return_value = 119.0
            with self.assertRaises(SystemExit) as exit:
                school.main()
            self.assertEqual(exit.exception.code,0)
        self.assertEqual([call.args[0]['uid'] for call in process.call_args_list], ['one','two'])
        sleep.assert_called_once_with(119.0)


if __name__ == '__main__':
    unittest.main()
