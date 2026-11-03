import argparse
import io
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack
from scripts_stability import ROOT, renew, jwt

REAL_ENSURE = renew.ensure_proxy


class RenewalStability(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='renew-offline-')))
        for name, value in {
            'HOME': self.root, 'BACKOFF_FILE': self.root / 'backoff',
            'LOCK_FILE': self.root / 'lock', 'TOKEN_FILE': self.root / 'token',
            # 續期時鐘也要隔離：不隔離就會讀到真實的 renew-watchdog.last-success，
            # 於是「距上次成功續期超過 25 分鐘」時這組測試會突然開始登入，
            # 通過與否取決於上一次真實續期是多久以前。
            'LAST_SUCCESS_FILE': self.root / 'last-success',
        }.items():
            self.stack.enter_context(patch.object(renew, name, value))
        self.stack.enter_context(patch.object(renew, 'log'))
        self.ensure = self.stack.enter_context(patch.object(renew, 'ensure_proxy', return_value=True))
        self.stack.enter_context(patch.object(renew, 'token_expiry', return_value=time.time()+10000))
        self.stack.enter_context(patch.object(renew, 'token_issued', return_value=time.time()))
        self.stack.enter_context(patch.object(renew, 'read_token', return_value='fake'))
        self.gateway = self.stack.enter_context(patch.object(renew, 'gateway_check', return_value=('ok', 'mock')))
        self.login = self.stack.enter_context(patch.object(renew, 'run_login', return_value=True))
        self.stack.enter_context(patch.object(renew.sys, 'argv', ['renew']))

    def test_overlapping_cycle_cannot_start_proxy(self):
        self.assertTrue(renew.acquire_lock(time.time()))
        try:
            self.assertEqual(renew.main(), 1)
            self.ensure.assert_not_called()
            self.gateway.assert_not_called()
        finally:
            renew.release_lock()

    def test_failure_releases_cycle_lock(self):
        self.ensure.side_effect = RuntimeError('failure')
        with self.assertRaises(RuntimeError):
            renew.main()
        self.assertTrue(renew.acquire_lock(time.time()))
        renew.release_lock()

    def test_unhealthy_proxy_is_reported_even_when_token_works(self):
        self.ensure.return_value = False
        self.assertEqual(renew.main(), 2)
        self.login.assert_not_called()

    def test_age_based_renewal_is_preserved(self):
        with patch.object(renew, 'last_renewal_time', return_value=time.time()-1600):
            self.assertEqual(renew.main(), 0)
        self.login.assert_called_once()

    def test_frozen_iat_alone_does_not_trigger_a_login(self):
        # A re-login for the same session returns the same token, so `iat` never
        # advances; keying the age rule on it made the watchdog log in (and pop a
        # window) every five minutes.
        with patch.object(renew, 'last_renewal_time', return_value=time.time()):
            self.assertAlmostEqual(renew.token_issued(), time.time(), delta=5)
            self.assertEqual(renew.main(), 0)
        self.login.assert_not_called()

    def test_failed_login_sets_backoff_and_next_cycle_does_not_login(self):
        self.login.return_value = False
        self.gateway.return_value = ('rejected', 'mock 401')
        self.assertEqual(renew.main(), 1)
        self.assertTrue(renew.BACKOFF_FILE.exists())
        self.assertEqual(renew.main(), 1)
        self.assertEqual(self.login.call_count, 1)

    def test_bad_backoff_and_clock_rollback_do_not_block_indefinitely(self):
        now = time.time()
        for value in ['nan', 'inf', 'garbage', str(now+100000)]:
            renew.BACKOFF_FILE.write_text(value)
            self.assertEqual(renew.in_backoff(now), 0)
        renew.BACKOFF_FILE.write_text(str(now-100))
        self.assertAlmostEqual(renew.in_backoff(now), renew.FAIL_BACKOFF_SECONDS-100)

    def test_bad_claim_times_are_rejected(self):
        for value in [10**1000, -1, float('nan'), 253402300800, True]:
            with patch.object(renew, 'token_claims', return_value={'exp': value}):
                self.assertIsNone(renew.claim_time('exp'))

    def test_occupied_unhealthy_port_does_not_spawn(self):
        with patch.object(renew, 'proxy_healthy', return_value=False), patch.object(renew.socket, 'create_connection', return_value=io.BytesIO()), patch.object(renew.subprocess, 'Popen') as spawn:
            self.assertFalse(REAL_ENSURE())
            spawn.assert_not_called()

    def test_early_exit_does_not_wait_for_startup_timeout(self):
        process = Mock(returncode=2)
        process.poll.return_value = 2
        with patch.object(renew, 'proxy_healthy', return_value=False), patch.object(renew.socket, 'create_connection', side_effect=ConnectionRefusedError), patch.object(Path, 'exists', return_value=True), patch.object(renew.subprocess, 'Popen', return_value=process), patch.object(renew.time, 'sleep') as sleep:
            self.assertFalse(REAL_ENSURE())
            sleep.assert_not_called()
            process.kill.assert_not_called()

    def test_timed_out_start_is_reaped(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(renew, 'proxy_healthy', return_value=False), patch.object(renew.socket, 'create_connection', side_effect=ConnectionRefusedError), patch.object(Path, 'exists', return_value=True), patch.object(renew.subprocess, 'Popen', return_value=process), patch.object(renew, 'PROXY_START_TIMEOUT', 0):
            self.assertFalse(REAL_ENSURE())
            process.kill.assert_called_once()
            process.wait.assert_called_once_with(timeout=5)

    def test_login_timeout_reaps_owned_parent(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired('login', 91), 1]
        with patch.object(renew.subprocess, 'Popen', return_value=process):
            with self.assertRaises(subprocess.TimeoutExpired):
                renew.login_process(['fake'], 1)
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_login_output_tail_is_bounded(self):
        def spawn(*args, **kwargs):
            kwargs['stdout'].write(b'x' * 100000)
            return Mock(wait=Mock(return_value=0))
        with patch.object(renew.subprocess, 'Popen', side_effect=spawn):
            result = renew.login_process(['fake'], 1)
        self.assertEqual(len(result.stdout), 16384)


class AgeRule(unittest.TestCase):
    def test_age_is_measured_from_the_last_renewal(self):
        now = 1_000_000.0
        exp = now + 10_000
        self.assertFalse(renew.needs_renewal(exp, now, now - 60))
        self.assertTrue(renew.needs_renewal(exp, now, now - renew.MAX_TOKEN_AGE_SECONDS))
        self.assertFalse(renew.needs_renewal(exp, now, None))
        self.assertTrue(renew.needs_renewal(now + 600, now, now))
        self.assertTrue(renew.needs_renewal(None, now, now))

    def test_the_recorded_renewal_becomes_the_clock(self):
        # A missing record falls back to the stored token's mtime; missing both
        # means there is no clock to age, not a crash.
        fixtures = ROOT / 'test' / 'fixtures'
        stored = fixtures / 'proxy.py'
        with patch.object(renew, 'LAST_SUCCESS_FILE', fixtures / 'missing-last-success'), \
                patch.object(renew, 'TOKEN_FILE', stored):
            self.assertEqual(renew.last_renewal_time(), stored.stat().st_mtime)
        with patch.object(renew, 'LAST_SUCCESS_FILE', fixtures / 'missing-last-success'), \
                patch.object(renew, 'TOKEN_FILE', fixtures / 'missing-token'):
            self.assertIsNone(renew.last_renewal_time())


class LoginWindowStability(unittest.TestCase):
    """run_login prefers the hidden desktop and only then shows a window."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {
            'SETUP_SCRIPT': Path('C:/nope/um_opencode_setup.py'),
            'VISIBLE_PROMPT_FILE': Path('C:/nope/visible-prompt'),
            'LAST_SUCCESS_FILE': Path('C:/nope/last-success'),
        }.items():
            self.stack.enter_context(patch.object(renew, name, value))
        self.stack.enter_context(patch.object(Path, 'exists', return_value=True))
        self.stack.enter_context(patch.object(renew, 'debug_port_alive', return_value=True))
        self.stack.enter_context(patch.object(renew, 'log'))
        self.release = self.stack.enter_context(patch.object(renew, 'release_hidden_login_window'))
        self.mark = self.stack.enter_context(patch.object(renew, 'mark_visible_prompt'))
        self.record = self.stack.enter_context(patch.object(renew, 'record_renewal'))

    def attempt(self, results):
        return self.stack.enter_context(patch.object(renew, 'attempt_login', side_effect=list(results)))

    def hidden(self, ok):
        return self.stack.enter_context(patch.object(renew, 'hidden_login_report', return_value={'ok': ok}))

    def test_successful_hidden_login_shows_no_window(self):
        self.hidden(True)
        attempt = self.attempt([True])
        self.assertTrue(renew.run_login(5))
        self.assertEqual(attempt.call_count, 1)
        self.release.assert_not_called()
        self.mark.assert_not_called()
        self.record.assert_called_once()

    def test_failed_hidden_login_shows_one_window_and_retries(self):
        self.hidden(True)
        self.stack.enter_context(patch.object(renew, 'visible_prompt_allowed', return_value=True))
        attempt = self.attempt([False, True])
        self.assertTrue(renew.run_login(5))
        self.assertEqual(attempt.call_count, 2)
        self.release.assert_called_once()
        self.mark.assert_called_once()

    def test_the_visible_retry_respects_its_cooldown(self):
        self.hidden(True)
        self.stack.enter_context(patch.object(renew, 'visible_prompt_allowed', return_value=False))
        attempt = self.attempt([False])
        self.assertFalse(renew.run_login(5))
        self.assertEqual(attempt.call_count, 1)
        self.release.assert_not_called()
        self.mark.assert_not_called()

    def test_without_a_hidden_browser_there_is_no_retry(self):
        self.hidden(False)
        attempt = self.attempt([False])
        self.assertFalse(renew.run_login(5))
        self.assertEqual(attempt.call_count, 1)
        self.release.assert_not_called()


if __name__ == '__main__':
    unittest.main()
