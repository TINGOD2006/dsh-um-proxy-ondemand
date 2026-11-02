"""Offline regressions; all credentials, browsers, network and taskkill are mocked."""
import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


renew, health, models = load("um-renew"), load("um-health"), load("um-models-sync")


def jwt(payload):
    return "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"


class Stability(unittest.TestCase):
    def setUp(self):
        self.proxy_patch = patch.object(renew, "ensure_proxy", return_value=True)
        self.proxy_patch.start()
        self.addCleanup(self.proxy_patch.stop)
        # main() tests must never acquire the user's real lock or read credentials.
        self.lock_patch = patch.object(renew, "acquire_lock", return_value=True)
        self.unlock_patch = patch.object(renew, "release_lock")
        if self._testMethodName == "test_network_failure_is_reported_without_renewal":
            self.lock_patch.start(); self.unlock_patch.start()
            self.addCleanup(self.lock_patch.stop); self.addCleanup(self.unlock_patch.stop)

    def test_jwt_malformed_payloads_do_not_crash(self):
        for payload in [[], None, "oops", {"exp": True}, {"exp": float("inf")}, {"exp": float("nan")}]:
            self.assertIsNone(health.jwt_expiry(jwt(payload)))
            with patch.object(renew, "read_token", return_value=jwt(payload)):
                self.assertIsNone(renew.token_expiry())

    def test_lock_failure_prevents_login(self):
        with patch("builtins.open", side_effect=PermissionError), patch.object(renew, "log"):
            self.assertFalse(renew.acquire_lock(time.time()))

    def test_os_lock_excludes_other_process_and_releases(self):
        with tempfile.TemporaryDirectory(prefix="dsh-renew-lock-") as directory:
            lock = Path(directory) / "renew.lock"
            with patch.object(renew, "HOME", Path(directory)), patch.object(renew, "LOCK_FILE", lock):
                self.assertTrue(renew.acquire_lock(time.time()))
                code = (
                    "import runpy,pathlib,time; "
                    f"m=runpy.run_path({str(ROOT / 'scripts' / 'um-renew.py')!r}); "
                    "f=m['acquire_lock']; "
                    f"f.__globals__['LOCK_FILE']=pathlib.Path({str(lock)!r}); "
                    f"f.__globals__['HOME']=pathlib.Path({directory!r}); "
                    "f.__globals__['log']=lambda m:None; "
                    "print(f(time.time()))"
                )
                try:
                    child = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=10)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    self.assertEqual(child.stdout.strip(), "False")
                finally:
                    renew.release_lock()
                self.assertTrue(renew.acquire_lock(time.time()))
                renew.release_lock()

    def test_private_profile_matching_is_exact(self):
        expected = str(renew.BROWSER_PROFILE)
        self.assertTrue(renew.is_private_login_process(f'chrome.exe --user-data-dir="{expected}"'))
        for command in [
            f'chrome.exe --user-data-dir="{expected}-other"',
            f'chrome.exe --user-data-dir="{expected}" --remote-debugging-port=1234',
            f'chrome.exe --user-data-dir="{expected}" --type=renderer',
            'chrome.exe --user-data-dir="C:\\unrelated\\browser-profile"',
        ]:
            self.assertFalse(renew.is_private_login_process(command), command)

    def test_chrome_cleanup_only_kills_exact_private_root(self):
        rows = [
            {"ProcessId": 101, "CommandLine": f'chrome.exe --user-data-dir="{renew.BROWSER_PROFILE}"'},
            {"ProcessId": 202, "CommandLine": 'chrome.exe --user-data-dir="C:\\other\\browser-profile"'},
        ]
        with patch.object(renew, "powershell", return_value=json.dumps(rows)), patch.object(renew.subprocess, "run") as run, patch.object(renew.time, "sleep"), patch.object(renew, "log"):
            renew.close_stale_login_window()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "101", "/T", "/F"])

    def test_unconfirmed_fresh_token_is_not_success(self):
        completed = subprocess.CompletedProcess([], 0, stdout=b"logged in")
        with patch.object(Path, "exists", return_value=True), patch.object(renew, "debug_port_alive", return_value=True), patch.object(renew, "hidden_login_report", return_value={"ok": False}), patch.object(renew, "login_process", return_value=completed), patch.object(renew, "read_token", return_value="fake"), patch.object(renew, "gateway_check", return_value=("error", "offline")), patch.object(renew, "log"):
            self.assertFalse(renew.run_login(1))

    def test_network_failure_is_reported_without_renewal(self):
        # last_renewal_time() reads the real ~/.um-opencode/renew-watchdog.last-success,
        # so without pinning it this only passes while that file is absent or older
        # than PROBE_ERROR_AGE_SECONDS: a recent renewal turns the "probe failed"
        # branch into "renew anyway" and main() returns 0 instead of 3.
        with patch.object(sys, "argv", ["um-renew.py"]), patch.object(renew, "token_expiry", return_value=time.time()+10000), patch.object(renew, "token_issued", return_value=None), patch.object(renew, "last_renewal_time", return_value=None), patch.object(renew, "read_token", return_value="fake"), patch.object(renew, "gateway_check", return_value=("error", "offline")), patch.object(renew, "run_login") as login, patch.object(renew, "log"):
            self.assertEqual(renew.main(), 3)
            login.assert_not_called()

    def test_gateway_probe_requires_a_real_model_catalog(self):
        for raw, expected in [(b'<html>login</html>', 'error'), (b'{"data":[]}', 'ok'), (b'{}', 'error')]:
            response = io.BytesIO(raw)
            response.status = 200
            with patch.object(renew.urllib.request, "urlopen", return_value=response):
                self.assertEqual(renew.gateway_check("fake", attempts=1)[0], expected)

    def test_health_http_503_is_failure_not_sleep(self):
        checks = health.Checks()
        error = urllib.error.HTTPError("http://127.0.0.1:1", 503, "busy", {}, None)
        with patch.object(health.urllib.request, "urlopen", side_effect=error):
            health.check_proxy(checks, "http://127.0.0.1:1/v1")
        self.assertEqual(checks.worst(), health.FAIL)

    def test_bad_health_response_becomes_failed_row(self):
        for payload in [b"broken", b"[]", b'{"proxy": []}', b'{"proxy":{"uptimeSeconds":"bad"}}']:
            checks = health.Checks()
            response = io.BytesIO(payload)
            response.status = 200
            with patch.object(health.urllib.request, "urlopen", return_value=response), patch.object(health, "read_text", return_value="fake"):
                callback = health.check_proxy if payload in [b"broken", b"[]"] else health.check_diagnostics
                args = ("http://127.0.0.1:1/v1",) if callback == health.check_proxy else ("http://127.0.0.1:1/v1", Path("fake"))
                health.run_check(checks, "response", callback, *args)
            self.assertEqual(checks.worst(), health.FAIL)

    def test_disabled_and_empty_tasks_are_warnings(self):
        for output in ['"task","2026/10/27, 01:00","Disabled"\n', '']:
            checks = health.Checks()
            with patch.object(health.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=output)):
                health.check_tasks(checks)
            self.assertEqual(checks.worst(), health.WARN)

    def test_sync_rolls_back_first_file_when_second_write_fails(self):
        with tempfile.TemporaryDirectory(prefix="dsh-model-sync-") as directory:
            files = [Path(directory) / name for name in ("first.json", "second.json")]
            for file in files:
                file.write_text("old", encoding="utf-8")
            original_write = models.write_text
            def failing(file, text):
                if file == files[1] and text == "new":
                    raise PermissionError("simulated second write failure")
                original_write(file, text)
            with patch.object(models, "write_text", side_effect=failing), patch.object(models, "log"):
                with self.assertRaises(models.SyncError):
                    models.apply_plans([(file, "old", [], "new") for file in files], None)
            self.assertEqual([file.read_text() for file in files], ["old", "old"])
            self.assertEqual(len(list(Path(directory).glob("*.bak-models-*"))), 2)

    def test_sync_detects_external_edits_before_writing(self):
        with tempfile.TemporaryDirectory(prefix="dsh-model-change-") as directory:
            file = Path(directory) / "config.json"
            file.write_text("user edit", encoding="utf-8")
            with self.assertRaises(models.SyncError):
                models.apply_plans([(file, "old", [], "new")], None)
            self.assertEqual(file.read_text(), "user edit")

    def test_sync_lock_rejects_a_second_writer(self):
        with tempfile.TemporaryDirectory(prefix="dsh-model-lock-") as directory:
            file = Path(directory) / "config.json"
            with models.config_lock(file):
                with self.assertRaises(models.SyncError):
                    with models.config_lock(file):
                        self.fail("second writer acquired an already held lock")
            with models.config_lock(file):
                pass

    def test_sync_invalid_json_and_catalog_shapes_are_controlled_errors(self):
        for raw in [b"invalid", b'{"data":{}}', b'"bad"']:
            response = io.BytesIO(raw)
            with patch.object(models.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(models.SyncError):
                    models.fetch_catalog("http://127.0.0.1:1/v1", "fake")

    def test_sync_rejects_duplicates_and_partial_invalid_catalogs(self):
        for payload in [[{"id": "same"}, {"id": "same"}], [{"id": "good"}, {"id": None}]]:
            with self.assertRaises(models.SyncError):
                models.validate_catalog(payload, "test")

    def test_gateway_can_revoke_registry_image_capability(self):
        registry = {"families": {"Example": {"attachment": True}}}
        info = {"info": {"meta": {"capabilities": {"vision": False, "file_upload": False, "file_context": False}}}}
        self.assertEqual(models.metadata_for("Example", registry, info, [])["input"], ["text"])
        self.assertEqual(models.metadata_for("Example", registry, {}, [])["input"], ["text", "image"])

    def test_check_mode_does_not_write_logs_locks_or_configs(self):
        with tempfile.TemporaryDirectory(prefix="dsh-check-") as directory:
            root = Path(directory)
            config = root / "opencode.json"
            original = '{"provider":{"um-genai":{"models":{}}}}'
            config.write_text(original, encoding="utf-8")
            registry = root / "registry.json"
            registry.write_text('{}', encoding="utf-8")
            key = root / "key"
            key.write_text("fake", encoding="utf-8")
            args = ["sync", "--check", "--target", "opencode", "--opencode", str(config), "--registry", str(registry), "--api-key-file", str(key), "--log", str(root / "sync.log")]
            with patch.object(sys, "argv", args), patch.object(models, "fetch_catalog", return_value=[{"id": "Example"}]), patch("sys.stdout", io.StringIO()):
                self.assertEqual(models.main(), 10)
            self.assertEqual(config.read_text(), original)
            self.assertEqual({file.name for file in root.iterdir()}, {"opencode.json", "registry.json", "key"})

    def test_failed_atomic_replace_preserves_original_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix="dsh-replace-") as directory:
            config = Path(directory) / "config.json"
            config.write_text("old", encoding="utf-8")
            with patch.object(models.os, "replace", side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    models.write_text(config, "new")
            self.assertEqual(config.read_text(), "old")
            self.assertEqual(list(Path(directory).iterdir()), [config])


if __name__ == "__main__":
    unittest.main()
