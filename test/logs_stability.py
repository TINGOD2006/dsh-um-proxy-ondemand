"""Log rotation and renewal-health checks; every path is a temporary directory."""
import argparse
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scripts_stability import logs, renew


class LogCase(unittest.TestCase):
    """Point every path the module knows at a throwaway directory."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="um-logs-")))
        self.watchdog = self.root / "renew-watchdog.log"
        self.models = self.root / "um-models-sync.log"
        self.plugin = self.root / "plugin.log"
        self.proxy = self.root / "proxy.log"
        self.success = self.root / "renew-watchdog.last-success"
        self.health = self.root / "log-health.json"
        for name, value in {
            "HOME": self.root,
            "LOG_FILE": self.watchdog,
            "LAST_SUCCESS_FILE": self.success,
            "HEALTH_FILE": self.health,
            # 1 KB instead of 8 MB so the tests do not have to write megabytes.
            "MAX_BYTES": 1024,
            "LOGS": (
                ("renew-watchdog.log", self.watchdog, True, "續期看門狗"),
                ("um-models-sync.log", self.models, True, "模型同步"),
                ("plugin.log", self.plugin, True, "插件"),
                ("proxy.log", self.proxy, False, "代理存取"),
            ),
        }.items():
            self.stack.enter_context(patch.object(logs, name, value))

    def backup(self, index):
        return self.root / f"renew-watchdog.log.{index}"


class Rotation(LogCase):
    def test_small_file_is_left_alone(self):
        self.watchdog.write_text("one\n", encoding="utf-8")
        result = logs.rotate(self.watchdog, max_bytes=1024)
        self.assertFalse(result["rotated"])
        self.assertEqual(result["reason"], "under-limit")
        self.assertTrue(self.watchdog.exists())
        self.assertFalse(self.backup(1).exists())

    def test_missing_file_is_not_an_error(self):
        result = logs.rotate(self.watchdog)
        self.assertFalse(result["rotated"])
        self.assertEqual(result["reason"], "missing")

    def test_rotation_keeps_three_backups_newest_first(self):
        for body in ("oldest", "middle", "newest", "fourth"):
            self.watchdog.write_text(body + "\n", encoding="utf-8")
            self.assertTrue(logs.rotate(self.watchdog, max_bytes=1)["rotated"])
        self.assertEqual(self.backup(1).read_text(encoding="utf-8"), "fourth\n")
        self.assertEqual(self.backup(2).read_text(encoding="utf-8"), "newest\n")
        self.assertEqual(self.backup(3).read_text(encoding="utf-8"), "middle\n")
        self.assertFalse(self.backup(4).exists())

    def test_proxy_log_is_never_rotated_by_the_watchdog(self):
        # The proxy rotates its own access log on start; renaming a file another
        # process holds open fails on Windows, so the watchdog must not try.
        self.proxy.write_text("x" * 2048, encoding="utf-8")
        names = [item["name"] for item in logs.rotate_all()]
        self.assertNotIn("proxy.log", names)
        self.assertTrue(self.proxy.exists())

    def test_rotate_all_reports_every_owned_file(self):
        self.watchdog.write_text("x" * 2048, encoding="utf-8")
        by_name = {item["name"]: item for item in logs.rotate_all()}
        self.assertTrue(by_name["renew-watchdog.log"]["rotated"])
        self.assertFalse(by_name["um-models-sync.log"]["rotated"])
        self.assertEqual(by_name["um-models-sync.log"]["reason"], "missing")

    def test_plugin_log_is_rotated_with_the_others(self):
        # The host appends plugin warnings; the watchdog owns the rotation.
        self.plugin.write_text("w" * 2048, encoding="utf-8")
        by_name = {item["name"]: item for item in logs.rotate_all()}
        self.assertTrue(by_name["plugin.log"]["rotated"])
        self.assertTrue((self.root / "plugin.log.1").exists())


class Snapshot(LogCase):
    def test_snapshot_describes_files_and_policy(self):
        self.watchdog.write_text("a\nb\n", encoding="utf-8")
        data = logs.snapshot()
        self.assertEqual(data["maxBytes"], logs.MAX_BYTES)
        self.assertEqual(data["backups"], 3)
        files = {item["name"]: item for item in data["files"]}
        self.assertTrue(files["renew-watchdog.log"]["exists"])
        self.assertEqual(files["renew-watchdog.log"]["lines"], 2)
        self.assertFalse(files["proxy.log"]["exists"])
        self.assertFalse(files["proxy.log"]["rotated"])

    def test_proxy_log_override_is_reported(self):
        custom = self.root / "custom-proxy.log"
        custom.write_text("x\n", encoding="utf-8")
        files = {item["name"]: item for item in logs.snapshot(proxy_log=custom)["files"]}
        self.assertEqual(files["proxy.log"]["path"], str(custom))
        self.assertTrue(files["proxy.log"]["exists"])

    def test_maintenance_writes_the_snapshot(self):
        self.watchdog.write_text("x" * 2048, encoding="utf-8")
        data = logs.maintenance(now=time.time())
        self.assertTrue(self.health.exists())
        saved = json.loads(self.health.read_text(encoding="utf-8"))
        self.assertEqual(saved["checkedAt"], data["checkedAt"])
        self.assertTrue(self.backup(1).exists())

    def test_snapshot_carries_its_own_staleness_limit(self):
        # 讀取端（設定頁、um-health）不該各自寫死 15 分鐘。
        data = logs.snapshot(now=1000.0)
        self.assertEqual(data["checkedAt"], 1000)
        self.assertEqual(data["staleAfterSeconds"], logs.SNAPSHOT_STALE_SECONDS)

    def test_optional_logs_are_marked_so_a_missing_file_is_not_an_alarm(self):
        # plugin.log 不存在＝還沒發生過警告，不是壞掉。
        files = {item["name"]: item for item in logs.snapshot()["files"]}
        self.assertTrue(files["plugin.log"]["optional"])
        self.assertFalse(files["plugin.log"]["exists"])
        self.assertFalse(files["renew-watchdog.log"]["optional"])


class SnapshotAge(unittest.TestCase):
    """凍住的快照看起來永遠是「沒有問題」，所以新鮮度只能由讀取端判斷。"""

    def test_a_fresh_snapshot_is_not_stale(self):
        now = 10_000.0
        result = logs.snapshot_age({"checkedAt": int(now) - 60, "staleAfterSeconds": 900}, now=now)
        self.assertEqual(result["ageSeconds"], 60)
        self.assertFalse(result["stale"])

    def test_three_missed_cycles_are_stale(self):
        now = 10_000.0
        result = logs.snapshot_age({"checkedAt": int(now) - 901, "staleAfterSeconds": 900}, now=now)
        self.assertTrue(result["stale"])

    def test_a_snapshot_without_a_timestamp_counts_as_stale(self):
        self.assertTrue(logs.snapshot_age({}, now=10_000.0)["stale"])
        self.assertIsNone(logs.snapshot_age({}, now=10_000.0)["ageSeconds"])
        self.assertTrue(logs.snapshot_age(None, now=10_000.0)["stale"])

    def test_a_missing_limit_falls_back_to_the_module_default(self):
        now = 10_000.0
        result = logs.snapshot_age({"checkedAt": int(now) - logs.SNAPSHOT_STALE_SECONDS - 1}, now=now)
        self.assertTrue(result["stale"])

    def test_a_clock_that_went_backwards_is_not_stale(self):
        now = 10_000.0
        result = logs.snapshot_age({"checkedAt": int(now) + 600, "staleAfterSeconds": 900}, now=now)
        self.assertEqual(result["ageSeconds"], 0)
        self.assertFalse(result["stale"])


class Health(LogCase):
    def write_failures(self, count, age_seconds=60):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_seconds))
        line = f"[{stamp}] auto-login did not produce a usable token; backing off"
        self.watchdog.write_text("\n".join([line] * count) + "\n", encoding="utf-8")

    def codes(self, now=None):
        return [problem["code"] for problem in logs.check(now=now or time.time())]

    def test_three_failures_in_a_day_are_reported(self):
        self.write_failures(3)
        self.assertIn("renewal-failures", self.codes())

    def test_two_failures_are_below_the_threshold(self):
        self.write_failures(2)
        self.assertNotIn("renewal-failures", self.codes())

    def test_old_failures_fall_outside_the_window(self):
        self.write_failures(3, age_seconds=48 * 3600)
        self.assertNotIn("renewal-failures", self.codes())

    def test_stale_last_success_is_reported(self):
        self.success.write_text("", encoding="utf-8")
        old = time.time() - 45 * 60
        os.utime(self.success, (old, old))
        self.assertIn("renewal-stale", self.codes())

    def test_fresh_last_success_is_quiet(self):
        self.success.write_text("", encoding="utf-8")
        self.assertNotIn("renewal-stale", self.codes())

    def test_missing_last_success_is_not_a_problem(self):
        # A machine that has never renewed has no clock to judge; the token
        # state itself is what the status panel reports.
        self.assertNotIn("renewal-stale", self.codes())

    def test_renewal_health_reads_the_last_success_file(self):
        self.success.write_text("", encoding="utf-8")
        health = logs.renewal_health(time.time())
        self.assertIsNotNone(health["lastSuccessAt"])
        self.assertLess(health["lastSuccessAgeSeconds"], 5)

    def test_rotation_error_is_reported(self):
        problems = logs.check(
            now=time.time(),
            rotations=[{"name": "renew-watchdog.log", "reason": "error: [WinError 32] 另一個程式正在使用這個檔案"}],
        )
        self.assertEqual(problems[0]["code"], "rotation-failed")
        self.assertIn("WinError 32", problems[0]["detail"])

    def test_one_failed_cycle_counts_once(self):
        # A real failed cycle writes two "auto-login failed with exit code 1"
        # (hidden, then the visible fallback), one terminal "did not produce",
        # and three "retrying in N min" lines while it backs off. Counting any
        # of the repeats would turn one failure into four and trip the alarm.
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 60))
        lines = [
            f"[{stamp}] auto-login failed with exit code 1",
            f"[{stamp}] auto-login failed with exit code 1",
            f"[{stamp}] auto-login did not produce a usable token; backing off before the next attempt",
            f"[{stamp}] token is due (valid until x) but the last attempt failed; retrying in 10 min",
            f"[{stamp}] token is due (valid until x) but the last attempt failed; retrying in 5 min",
            f"[{stamp}] token is due (valid until x) but the last attempt failed; retrying in 0 min",
        ]
        self.watchdog.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(logs.renewal_health(time.time())["failures24h"], 1)
        self.assertNotIn("renewal-failures", self.codes())


class WatchdogIntegration(unittest.TestCase):
    def test_cycle_tidies_logs_before_anything_else(self):
        args = argparse.Namespace(force=False, timeout=1)
        with patch.object(renew, "tidy_logs") as tidy, \
                patch.object(renew, "ensure_proxy", return_value=True), \
                patch.object(renew, "token_expiry", return_value=time.time() + 10000), \
                patch.object(renew, "last_renewal_time", return_value=time.time()), \
                patch.object(renew, "gateway_check", return_value=("ok", "mock")), \
                patch.object(renew, "read_token", return_value="fake"):
            self.assertEqual(renew.watchdog_cycle(args), 0)
        tidy.assert_called_once()

    def test_a_broken_log_module_never_stops_a_renewal(self):
        with patch.object(renew, "um_logs") as broken, patch.object(renew, "log") as written:
            broken.maintenance.side_effect = OSError("disk full")
            renew.tidy_logs()
        written.assert_called_once()
        self.assertIn("log maintenance failed", written.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
