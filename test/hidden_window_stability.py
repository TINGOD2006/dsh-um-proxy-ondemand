"""Offline regressions for the hidden login browser: no Chrome, no Win32 calls."""
import importlib.util
import pathlib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


window = load("um_hidden_window")


class PortFile(unittest.TestCase):
    def test_only_one_valid_port_is_accepted(self):
        for text in ["9333\n", " 9444 ", "1", "65535"]:
            self.assertIsNotNone(window.port_from_text(text), text)
        for text in ["", "abc", "0", "65536", "-1", "1 2", "9333.5", None, 9333, b"9333"]:
            self.assertIsNone(window.port_from_text(text), repr(text))

    def test_missing_port_file_is_not_an_error(self):
        missing = pathlib.Path("C:/definitely/not/here/browser-debug-port")
        self.assertIsNone(window.read_port(missing))
        self.assertIsNone(window.read_pid(missing))

    def test_pid_text_rejects_zero_and_junk(self):
        self.assertEqual(window._pid_from_text("4242\n"), 4242)
        for text in ["", "0", "-4", "junk", "4.2"]:
            self.assertIsNone(window._pid_from_text(text), repr(text))


class CommandLine(unittest.TestCase):
    def test_arguments_keep_the_login_window_off_screen(self):
        args = window.chrome_arguments(9333, pathlib.Path(r"C:\home\.um-opencode\browser-profile"),
                                       "https://chat.genai.um.edu.mo/auth")
        self.assertIn("--remote-debugging-port=9333", args)
        self.assertIn(r"--user-data-dir=C:\home\.um-opencode\browser-profile", args)
        self.assertIn("--window-position=-32000,-32000", args)
        self.assertEqual(args[-1], "https://chat.genai.um.edu.mo/auth")
        for shown in ("--new-window", "--start-maximized", "--headless=new"):
            self.assertNotIn(shown, args)

    def test_profile_is_read_from_quoted_and_unquoted_forms(self):
        for command in [
            'chrome.exe --user-data-dir="C:\\home\\browser-profile" --remote-debugging-port=9333',
            'chrome.exe --user-data-dir C:\\home\\browser-profile --remote-debugging-port=9333',
        ]:
            self.assertEqual(window.profile_from_command_line(command), r"C:\home\browser-profile")

    def test_two_profiles_or_none_are_not_attributed(self):
        self.assertIsNone(window.profile_from_command_line("chrome.exe"))
        self.assertIsNone(window.profile_from_command_line(""))
        ambiguous = 'chrome.exe --user-data-dir="C:\\a" --user-data-dir="C:\\b"'
        self.assertIsNone(window.profile_from_command_line(ambiguous))

    def test_same_path_ignores_case_and_relative_noise(self):
        self.assertTrue(window.same_path(r"C:\Home\Browser-Profile", r"c:\home\browser-profile"))
        self.assertFalse(window.same_path(r"C:\home\browser-profile", r"C:\home\browser-profile-2"))


class ProfilePids(unittest.TestCase):
    profile = pathlib.Path(r"C:\home\.um-opencode\browser-profile")

    def test_only_the_root_browser_of_this_profile_counts(self):
        rows = [
            {"ProcessId": 101, "CommandLine": f'chrome.exe --user-data-dir="{self.profile}" '
                                              "--remote-debugging-port=9333"},
            {"ProcessId": 102, "CommandLine": f'chrome.exe --user-data-dir="{self.profile}" '
                                              "--type=renderer"},
            {"ProcessId": 103, "CommandLine": 'chrome.exe --user-data-dir="C:\\other\\browser-profile"'},
            {"ProcessId": True, "CommandLine": f'chrome.exe --user-data-dir="{self.profile}"'},
            {"ProcessId": 0, "CommandLine": f'chrome.exe --user-data-dir="{self.profile}"'},
            {"CommandLine": f'chrome.exe --user-data-dir="{self.profile}"'},
            "not a row",
        ]
        self.assertEqual(window.profile_pids(rows, self.profile), [101])

    def test_single_object_and_empty_payloads_are_tolerated(self):
        row = {"ProcessId": 7, "CommandLine": f'msedge.exe --user-data-dir="{self.profile}"'}
        self.assertEqual(window.profile_pids(row, self.profile), [7])
        for payload in [None, [], {}, "junk", 3]:
            self.assertEqual(window.profile_pids(payload, self.profile), [], repr(payload))


class Reuse(unittest.TestCase):
    def test_a_recorded_port_counts_only_when_devtools_answers(self):
        self.assertTrue(window.should_reuse(9333, {"Browser": "Chrome/141"}))
        self.assertTrue(window.should_reuse(9333, {"webSocketDebuggerUrl": "ws://x"}))
        self.assertFalse(window.should_reuse(9333, None))
        self.assertFalse(window.should_reuse(9333, {}))
        self.assertFalse(window.should_reuse(None, {"Browser": "Chrome/141"}))
        self.assertFalse(window.should_reuse(9333, []))

    def test_no_browser_is_not_a_crash(self):
        self.assertEqual(window.browser_candidates("no-such-browser"), [])
        self.assertEqual(window.devtools_version(None), None)
        self.assertEqual(window.devtools_targets(None), [])

    def test_desktop_names_and_urls_are_overridable(self):
        self.assertTrue(window.DESKTOP_NAME)
        self.assertTrue(window.UPSTREAM_BASE.startswith("http"))


class DesktopLifetime(unittest.TestCase):
    """The desktop must outlive the launch, or the browser dies at startup.

    ``CreateProcessW`` returns before the new process has opened the desktop it
    was told to use. Closing the handle right away destroys that desktop under
    the browser, which then exits with 0xC06D007E before publishing a DevTools
    port - every hidden login failed this way, and the failure looked exactly
    like "Chrome would not start".
    """

    def setUp(self):
        self.closed = []
        self.published = []
        patcher = mock.patch.multiple(
            window,
            open_hidden_desktop=mock.Mock(return_value=77),
            close_desktop=mock.Mock(side_effect=lambda handle: self.closed.append(handle)),
            launch_on_desktop=mock.Mock(return_value=(4242, 11, 12)),
            browser_candidates=mock.Mock(return_value=[("chrome", pathlib.Path("chrome.exe"))]),
            wait_for_devtools=mock.Mock(side_effect=self._wait),
            close_handle=mock.Mock(),
            write_text=mock.Mock(),
            read_port=mock.Mock(return_value=None),
            read_pid=mock.Mock(return_value=None),
            devtools_version=mock.Mock(return_value=None),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait(self, port, timeout):
        self.published.append(list(self.closed))
        return {"Browser": "Chrome/141"}

    def test_the_desktop_stays_open_while_the_browser_starts(self):
        report = window.ensure_hidden_chrome(timeout=1.0)
        self.assertTrue(report["ok"])
        self.assertEqual(self.published, [[]], "desktop was closed before the browser came up")
        self.assertEqual(self.closed, [77])

    def test_the_desktop_is_closed_even_when_the_browser_never_answers(self):
        with mock.patch.object(window, "wait_for_devtools", return_value=None), \
             mock.patch.object(window, "kill_tree", return_value=True), \
             mock.patch.object(window, "profile_browser_pids", return_value=[]), \
             mock.patch.object(window.time, "sleep", return_value=None):
            report = window.ensure_hidden_chrome(timeout=1.0)
        self.assertFalse(report["ok"])
        self.assertEqual(self.closed, [77, 77], "one leaked desktop handle per attempt")

    def test_the_command_line_survives_awkward_paths(self):
        # list2cmdline is what CreateProcessW expects; naive space-splitting is not.
        line = window.subprocess.list2cmdline([r"C:\Program Files\Google\chrome.exe",
                                               r"--user-data-dir=C:\a b\profile"])
        self.assertEqual(line, r'"C:\Program Files\Google\chrome.exe" '
                                r'"--user-data-dir=C:\a b\profile"')


if __name__ == "__main__":
    unittest.main()
