import argparse
import io
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack
from scripts_stability import renew, jwt

REAL_ENSURE = renew.ensure_proxy


class RenewalStability(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='renew-offline-')))
        for name, value in {
            'HOME': self.root, 'BACKOFF_FILE': self.root / 'backoff',
            'LOCK_FILE': self.root / 'lock', 'TOKEN_FILE': self.root / 'token',
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
        with patch.object(renew, 'token_issued', return_value=time.time()-1600):
            self.assertEqual(renew.main(), 0)
        self.login.assert_called_once()

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


if __name__ == '__main__':
    unittest.main()
