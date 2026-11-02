#!/usr/bin/env python
"""Run the UM login browser on a hidden Windows desktop.

The auto-login script opens a real Chrome window whenever it has to sign the
session in again.  On Windows a window can be created on a desktop that nobody
is looking at: a private desktop made by ``CreateDesktopW`` plus
``STARTUPINFO.lpDesktop`` gives the process a normal interactive window that is
never visible on the user's screen.  Chrome on that desktop keeps working - it
renders, it runs JavaScript, and its DevTools endpoint answers on localhost - so
the login can be driven by the auto-login script exactly as before.

The login script already supports being handed a browser:

  * ``open_login_window()`` returns None as soon as ``existing_debug_port()``
    reports a usable browser, so no second window is opened, and
  * ``auto_login()`` attaches to that browser instead of launching one.

Both read ``~/.um-opencode/browser-debug-port``.  So all this module has to do
is put a Chrome on a hidden desktop and publish its port there; the login
script does the rest, and the proxy's own 401 fallback path gets the same
treatment for free.

Usage:
    python scripts/um_hidden_window.py --status   # is a hidden browser alive?
    python scripts/um_hidden_window.py --ensure   # start one if needed
    python scripts/um_hidden_window.py --close    # stop it and forget the port
    python scripts/um_hidden_window.py --probe    # self-test, then clean up
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = pathlib.Path(os.environ.get("UM_OPENCODE_HOME", pathlib.Path.home() / ".um-opencode"))
BROWSER_PROFILE = HOME / "browser-profile"
DEBUG_PORT_FILE = HOME / "browser-debug-port"
PID_FILE = HOME / "hidden-window.pid"

UPSTREAM_BASE = os.environ.get("UM_UPSTREAM_BASE", "https://chat.genai.um.edu.mo")
DESKTOP_NAME = os.environ.get("UM_HIDDEN_DESKTOP", "um-login-hidden")
DEFAULT_HOST = "127.0.0.1"

STARTUP_TIMEOUT = 30.0
POLL_INTERVAL = 0.4
# Second line of defence: a window that somehow lands on the interactive desktop
# anyway is parked far outside the virtual screen instead of on the user's face.
WINDOW_POSITION = "-32000,-32000"
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
GENERIC_ALL = 0x000F01FF

BROWSER_CANDIDATES = (
    (
        "chrome",
        (
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    (
        "edge",
        (
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
)


def log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------
# pure helpers (unit-tested without touching Win32)


def port_from_text(text: str) -> int | None:
    """Read a debug-port file body; anything that is not one TCP port is None."""
    if not isinstance(text, str):
        return None
    try:
        port = int(text.strip())
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def read_port(path: pathlib.Path = DEBUG_PORT_FILE) -> int | None:
    try:
        return port_from_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def write_text(path: pathlib.Path, body: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return True
    except OSError:
        return False


def chrome_arguments(port: int, profile: pathlib.Path, url: str) -> list[str]:
    """The login window's own argument list, minus anything that shows a window."""
    return [
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        f"--window-position={WINDOW_POSITION}",
        "--window-size=1280,900",
        url,
    ]


def profile_from_command_line(text: str) -> str | None:
    """The ``--user-data-dir`` value of a Chrome command line, when there is one."""
    if not isinstance(text, str):
        return None
    found = []
    tokens = text.split()
    for index, token in enumerate(tokens):
        value = None
        if token == "--user-data-dir" and index + 1 < len(tokens):
            value = tokens[index + 1]
        elif token.startswith("--user-data-dir="):
            value = token.split("=", 1)[1]
        if value is None:
            continue
        found.append(value.strip('"'))
    return found[0] if len(found) == 1 else None


def same_path(left: str | pathlib.Path, right: str | pathlib.Path) -> bool:
    try:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )
    except (OSError, TypeError, ValueError):
        return False


def profile_pids(rows: object, profile: pathlib.Path) -> list[int]:
    """Root browser processes that own *profile*; renderer children are skipped."""
    if not isinstance(rows, list):
        rows = [rows]
    pids: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        command = row.get("CommandLine")
        pid = row.get("ProcessId")
        if type(pid) is not int or pid <= 0 or not isinstance(command, str):
            continue
        if "--type=" in command:
            continue
        owner = profile_from_command_line(command)
        if owner is not None and same_path(owner, profile):
            pids.append(pid)
    return pids


def should_reuse(port: int | None, version: object) -> bool:
    """A recorded port is only reused when its DevTools endpoint really answers."""
    return bool(port) and isinstance(version, dict) and bool(version.get("Browser") or version.get("webSocketDebuggerUrl"))


def browser_candidates(preferred: str = "auto") -> list[tuple[str, pathlib.Path]]:
    wanted = (preferred or "auto").strip().lower()
    found: list[tuple[str, pathlib.Path]] = []
    for name, paths in BROWSER_CANDIDATES:
        if wanted not in ("auto", name):
            continue
        for raw in paths:
            candidate = pathlib.Path(os.path.expandvars(raw))
            if candidate.exists():
                found.append((name, candidate))
                break
    return found


def free_local_port(host: str = DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


# --------------------------------------------------------------------------
# Win32 plumbing


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def open_hidden_desktop(name: str = DESKTOP_NAME) -> int:
    """Create (or reopen) the private desktop the login window lives on."""
    handle = _user32().CreateDesktopW(ctypes.c_wchar_p(name), None, None, 0, GENERIC_ALL, None)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"CreateDesktopW({name!r}) failed")
    return handle


def close_desktop(handle: int) -> None:
    if handle:
        try:
            _user32().CloseDesktop(handle)
        except OSError:
            pass


def launch_on_desktop(executable: pathlib.Path, arguments: list[str], desktop: str) -> tuple[int, int, int]:
    """Start *executable* with its windows on *desktop*; returns (pid, hproc, hthread)."""
    # list2cmdline applies the same quoting rules CreateProcessW expects, so a
    # profile path with spaces or an embedded quote cannot split into extra argv.
    command = subprocess.list2cmdline([str(executable)] + arguments)
    startup = _StartupInfo()
    startup.cb = ctypes.sizeof(_StartupInfo)
    startup.lpDesktop = ctypes.c_wchar_p(desktop)
    info = _ProcessInformation()
    created = _kernel32().CreateProcessW(
        ctypes.c_wchar_p(str(executable)),
        ctypes.c_wchar_p(command),
        None,
        None,
        False,
        CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT | DETACHED_PROCESS,
        None,
        ctypes.c_wchar_p(str(executable.parent)),
        ctypes.byref(startup),
        ctypes.byref(info),
    )
    if not created:
        raise OSError(ctypes.get_last_error(), f"CreateProcessW({executable.name}) failed")
    return int(info.dwProcessId), int(info.hProcess or 0), int(info.hThread or 0)


def close_handle(handle: int) -> None:
    if handle:
        try:
            _kernel32().CloseHandle(handle)
        except OSError:
            pass


def desktop_windows(desktop: str = DESKTOP_NAME) -> list[str]:
    """Window titles currently living on the hidden desktop."""
    handle = 0
    titles: list[str] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _param):
        buffer = ctypes.create_unicode_buffer(256)
        _user32().GetWindowTextW(hwnd, buffer, 256)
        if buffer.value:
            titles.append(buffer.value)
        return True

    callback = callback_type(collect)
    try:
        handle = _user32().OpenDesktopW(ctypes.c_wchar_p(desktop), 0, False, GENERIC_ALL)
        if not handle:
            return []
        _user32().EnumDesktopWindows(handle, callback, 0)
    finally:
        close_desktop(handle)
    return titles


def devtools_version(port: int | None, timeout: float = 1.5) -> dict | None:
    if not port:
        return None
    try:
        with urllib.request.urlopen(f"http://{DEFAULT_HOST}:{port}/json/version", timeout=timeout) as response:
            payload = json.loads(response.read(65537).decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def devtools_targets(port: int | None, timeout: float = 1.5) -> list[dict]:
    if not port:
        return []
    try:
        with urllib.request.urlopen(f"http://{DEFAULT_HOST}:{port}/json/list", timeout=timeout) as response:
            payload = json.loads(response.read(1024 * 1024 + 1).decode("utf-8", "replace") or "[]")
    except (urllib.error.URLError, OSError, ValueError, UnicodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def wait_for_devtools(port: int, timeout: float = STARTUP_TIMEOUT) -> dict | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        version = devtools_version(port)
        if version:
            return version
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL)


def kill_tree(pid: int, timeout: float = 20.0) -> bool:
    if not pid:
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def powershell(script: str, timeout: float = 30.0) -> str:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); " + script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip()


def profile_browser_pids(profile: pathlib.Path = BROWSER_PROFILE) -> list[int]:
    """Root browser processes holding *profile* (used to clear a stuck profile)."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    raw = powershell(script) or "[]"
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return profile_pids(rows, profile)


# --------------------------------------------------------------------------
# public entry points


def status(port_file: pathlib.Path = DEBUG_PORT_FILE, pid_file: pathlib.Path = PID_FILE) -> dict:
    port = read_port(port_file)
    version = devtools_version(port)
    report = {
        "ok": should_reuse(port, version),
        "port": port,
        "pid": read_pid(pid_file),
        "browser": (version or {}).get("Browser"),
        "desktop": DESKTOP_NAME,
        "desktopWindows": desktop_windows() if version else [],
    }
    return report


def read_pid(pid_file: pathlib.Path = PID_FILE) -> int | None:
    try:
        return _pid_from_text(pid_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def _pid_from_text(text: str) -> int | None:
    try:
        pid = int(text.strip())
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def ensure_hidden_chrome(
    *,
    profile: pathlib.Path = BROWSER_PROFILE,
    port_file: pathlib.Path = DEBUG_PORT_FILE,
    pid_file: pathlib.Path = PID_FILE,
    url: str | None = None,
    browser: str = "auto",
    timeout: float = STARTUP_TIMEOUT,
    echo=log,
) -> dict:
    """Make sure a browser with a debug port runs on the hidden desktop."""
    report: dict = {"ok": False, "reused": False, "port": None, "pid": None,
                    "browser": None, "version": None, "error": None}
    recorded = read_port(port_file)
    existing = devtools_version(recorded)
    if should_reuse(recorded, existing):
        report.update(ok=True, reused=True, port=recorded,
                      pid=read_pid(pid_file), browser=existing.get("Browser"),
                      version=existing)
        return report

    candidates = browser_candidates(browser)
    if not candidates:
        report["error"] = "no Chrome or Edge executable found"
        return report
    name, executable = candidates[0]
    target = url or f"{UPSTREAM_BASE}/auth"

    for attempt in range(2):
        port = free_local_port()
        desktop = 0
        try:
            desktop = open_hidden_desktop(DESKTOP_NAME)
            pid, process_handle, thread_handle = launch_on_desktop(
                executable, chrome_arguments(port, profile, target), DESKTOP_NAME
            )
        except OSError as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            close_desktop(desktop)
            return report
        # The desktop handle has to stay open until the browser is far enough along
        # to hold its own reference to it. Closing it right after CreateProcessW
        # destroys the desktop under a process that has not opened it yet, and the
        # new browser dies with 0xC06D007E before it ever publishes its port -
        # which is exactly how every hidden login failed until this was fixed.
        try:
            version = wait_for_devtools(port, timeout)
        finally:
            close_desktop(desktop)
        if version:
            close_handle(process_handle)
            close_handle(thread_handle)
            write_text(port_file, f"{port}\n")
            write_text(pid_file, f"{pid}\n")
            report.update(ok=True, port=port, pid=pid, browser=name, version=version)
            return report
        # A browser already owning the profile only hands the URL over and never
        # publishes the port; clear it and try once more on a fresh desktop.
        close_handle(process_handle)
        close_handle(thread_handle)
        kill_tree(pid)
        if attempt:
            break
        stale = profile_browser_pids(profile)
        if stale:
            echo(f"closing browser holding the private profile: pid {', '.join(map(str, stale))}")
            for other in stale:
                kill_tree(other)
            time.sleep(2.0)
    report["error"] = "the browser did not publish a DevTools port"
    return report


def stop_hidden_chrome(
    *,
    profile: pathlib.Path = BROWSER_PROFILE,
    port_file: pathlib.Path = DEBUG_PORT_FILE,
    pid_file: pathlib.Path = PID_FILE,
) -> dict:
    stopped: list[int] = []
    pid = read_pid(pid_file)
    if pid and kill_tree(pid):
        stopped.append(pid)
    recorded = read_port(port_file)
    if recorded:
        for other in profile_browser_pids(profile):
            if other not in stopped and kill_tree(other):
                stopped.append(other)
    for path in (port_file, pid_file):
        try:
            path.unlink()
        except OSError:
            pass
    return {"stopped": stopped, "cleared": [str(port_file), str(pid_file)]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="report the hidden browser and exit")
    parser.add_argument("--ensure", action="store_true", help="start the hidden browser when needed")
    parser.add_argument("--close", action="store_true", help="stop the hidden browser")
    parser.add_argument("--probe", action="store_true", help="start, describe and then stop it")
    parser.add_argument("--keep", action="store_true", help="with --probe, leave the browser running")
    parser.add_argument("--browser", default="auto", help="chrome, edge or auto")
    parser.add_argument("--timeout", type=float, default=STARTUP_TIMEOUT)
    args = parser.parse_args(argv)

    if args.close:
        print(json.dumps(stop_hidden_chrome(), ensure_ascii=False, indent=2))
        return 0

    if args.status:
        report = status()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    report = ensure_hidden_chrome(browser=args.browser, timeout=args.timeout)
    if report["ok"]:
        report["targets"] = [
            {"type": item.get("type"), "url": str(item.get("url", ""))[:120]}
            for item in devtools_targets(report["port"])
        ]
        report["desktopWindows"] = desktop_windows()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.probe and not args.keep:
        print(json.dumps(stop_hidden_chrome(), ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
