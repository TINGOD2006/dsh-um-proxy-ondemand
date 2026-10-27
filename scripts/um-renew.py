#!/usr/bin/env python
"""Keep the UM GenAI login token alive without waiting for a failed request.

The on-demand proxy only exists while it is being used, so the token watchdog it
runs itself cannot be relied on: while the proxy is asleep nothing notices that
the JWT expired, and the first request afterwards comes back 401 (the proxy does
try to renew inline, but that needs a browser window and often loses the race).

This watcher is independent of the proxy. Task Scheduler runs it every few
minutes; it decides from two independent signals:

  * the JWT `exp` claim (local clock, optimistic), and
  * a real `GET /api/models` against the UM gateway with the stored token
    (authoritative: the gateway can reject a token whose `exp` is still hours
    away - a just-renewed token was rejected after about an hour of idling, see
    renew-watchdog.log 2026-10-25).

Renewal runs the auto-login script:

    C:\\Python314\\python.exe C:\\Users\\TINGOD\\.um-opencode\\um_opencode_setup.py login --timeout 120

A login is only reported as successful when the fresh token is accepted by the
gateway. If a leftover Chrome window owns the private profile without exposing a
debug port (the failure mode that blocked every renewal on 2026-10-25), the stale
window is closed first so a new one can open.

Every decision and every outcome is appended to renew-watchdog.log. When the
token is still accepted the run costs one file read plus one small GET and exits
quietly.

Usage:
    python scripts/um-renew.py --status    # show token state, change nothing
    python scripts/um-renew.py --probe     # ask the gateway whether the token still works
    python scripts/um-renew.py --force     # run the login now
    python scripts/um-renew.py             # what Task Scheduler runs
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import ctypes
import shlex
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = pathlib.Path(os.environ.get("UM_OPENCODE_HOME", pathlib.Path.home() / ".um-opencode"))
TOKEN_FILE = HOME / "upstream-token"
SETUP_SCRIPT = HOME / "um_opencode_setup.py"
BROWSER_PROFILE = HOME / "browser-profile"
DEBUG_PORT_FILE = HOME / "browser-debug-port"
LOG_FILE = HOME / "renew-watchdog.log"
LOCK_FILE = HOME / "renew-watchdog.lock"
BACKOFF_FILE = HOME / "renew-watchdog.last-fail"

UPSTREAM_BASE = os.environ.get("UM_UPSTREAM_BASE", "https://chat.genai.um.edu.mo")
PROBE_PATH = "/api/models"
PROBE_TIMEOUT = 15.0

LEAD_SECONDS = 20 * 60  # renew this long before the JWT expires
# The gateway stops honouring a session token long before the JWT's exp says so:
# 2026-10-25 13:07 issued -> rejected from 14:08 (61 min), 2026-10-27 16:52 ->
# rejected by 17:52 (60 min), 2026-10-27 17:57 -> rejected by 18:27 (30 min),
# all with a ~9 hour exp claim. Refresh on age, not on exp, so a server-side
# cutoff never turns into a 401 the user has to notice. 25 minutes keeps the
# session fresh even for the shortest cutoff seen so far, and the gateway probe
# below still catches any shorter one.
MAX_TOKEN_AGE_SECONDS = 25 * 60
# A probe that only times out (gateway hiccup) must not postpone a refresh that
# the token's age already justifies, so age wins once the retries are spent.
PROBE_ERROR_AGE_SECONDS = 15 * 60
PROXY_PORT = int(os.environ.get("UM_PROXY_PORT", "8788"))
PROXY_HEALTH_TIMEOUT = 2.0
PROXY_START_TIMEOUT = 30.0
LOGIN_TIMEOUT_SECONDS = 120
FAIL_BACKOFF_SECONDS = 15 * 60  # do not retry a failed login more often than this


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def python_executable() -> str:
    """python.exe rather than pythonw.exe: the setup script prints a lot, and
    print() into a windowless interpreter (sys.stdout is None) would raise."""
    exe = pathlib.Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def read_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def token_claims() -> dict:
    token = read_token()
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def token_expiry() -> float | None:
    exp = token_claims().get("exp")
    return float(exp) if type(exp) in (int, float) and math.isfinite(exp) else None


def token_issued() -> float | None:
    iat = token_claims().get("iat")
    return float(iat) if type(iat) in (int, float) and math.isfinite(iat) else None


def local_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def describe(exp: float | None, now: float) -> str:
    if exp is None:
        return "no readable JWT in upstream-token"
    remaining = exp - now
    if remaining <= 0:
        return f"expired {local_time(exp)} ({int(-remaining // 60)} min ago)"
    return f"valid until {local_time(exp)} ({int(remaining // 60)} min left)"


def gateway_check(token: str, attempts: int = 2) -> tuple[str, str]:
    """Ask the UM gateway whether it still accepts this token.

    Returns (state, detail) where state is "ok", "rejected" or "error".
    "error" means the network or gateway is unavailable: that is not the token's
    fault, so the caller must not trigger a renewal for it. A transient timeout
    is retried once: on 2026-10-27 a single 15 s timeout at 17:52 was enough to
    skip the refresh that the expired session needed.
    """
    state, detail = ("error", "not attempted")
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(1.5)
        state, detail = _gateway_check_once(token)
        if state != "error":
            return state, detail
    return state, detail


def _gateway_check_once(token: str) -> tuple[str, str]:
    if not token:
        return "rejected", "no token stored"
    request = urllib.request.Request(f"{UPSTREAM_BASE}{PROBE_PATH}")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", "UMOpenCodeProxy/1.2")
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            if response.status != 200:
                return "error", f"HTTP {response.status}"
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                return "error", "gateway model catalog exceeds 2 MiB"
            payload = json.loads(raw.decode("utf-8"))
            items = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                return "error", "gateway did not return a model catalog"
            return "ok", f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            return "rejected", f"HTTP {exc.code}"
        return "error", f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError, UnicodeError) as exc:
        return "error", f"{type(exc).__name__}: {exc}"


def needs_renewal(exp: float | None, now: float, issued: float | None = None) -> bool:
    """True when the token should be replaced before the next request needs it."""
    if exp is None or exp - now <= LEAD_SECONDS:
        return True
    # exp is optimistic: the gateway drops sessions well before that.
    return issued is not None and now - issued >= MAX_TOKEN_AGE_SECONDS


_lock_handle = None


def acquire_lock(now: float) -> bool:
    """Use an OS lock: crashes release it, and age never evicts a live owner.

    The lock file deliberately persists; unlinking a locked file allows a second
    inode (and a second concurrent login) on POSIX.
    """
    global _lock_handle
    if _lock_handle is not None:
        return False
    handle = None
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        handle = open(LOCK_FILE, "a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle = handle
        return True
    except OSError as exc:
        if handle is not None:
            handle.close()
        log(f"renewal lock unavailable ({type(exc).__name__}); skipping login")
        return False


def release_lock() -> None:
    global _lock_handle
    if _lock_handle is not None:
        _lock_handle.close()
        _lock_handle = None


def in_backoff(now: float) -> float:
    """Seconds left of the failure backoff, 0 when a retry is allowed."""
    try:
        failed_at = float(BACKOFF_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0
    return max(0.0, FAIL_BACKOFF_SECONDS - (now - failed_at))


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


def debug_port_alive() -> bool:
    try:
        port = int(DEBUG_PORT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2.0):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def command_args(command: str) -> list[str]:
    if os.name != "nt":
        return [part.strip('"') for part in shlex.split(command, posix=False)]
    from ctypes import wintypes
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    pointer = parse(command, ctypes.byref(count))
    if not pointer:
        return []
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        free = ctypes.windll.kernel32.LocalFree
        free.argtypes = [wintypes.HLOCAL]
        free.restype = wintypes.HLOCAL
        free(ctypes.cast(pointer, wintypes.HLOCAL))


def is_private_login_process(command: str) -> bool:
    args = command_args(command)
    profiles = []
    for index, arg in enumerate(args):
        if arg.startswith("--type=") or arg == "--type":
            return False  # only the browser root, never renderer/utility children
        if arg.startswith("--remote-debugging-port") or arg == "--remote-debugging-pipe":
            return False
        if arg == "--user-data-dir" and index + 1 < len(args):
            profiles.append(args[index + 1])
        elif arg.startswith("--user-data-dir="):
            profiles.append(arg.split("=", 1)[1])
    expected = os.path.normcase(os.path.abspath(BROWSER_PROFILE))
    return len(profiles) == 1 and os.path.normcase(os.path.abspath(profiles[0])) == expected


def close_stale_login_window() -> None:
    """Only close Chrome roots using this exact private profile, without debug."""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        data = json.loads(powershell(script) or "[]")
        rows = data if isinstance(data, list) else [data]
    except (ValueError, TypeError):
        return
    pids = [row.get("ProcessId") for row in rows if isinstance(row, dict)
            and isinstance(row.get("CommandLine"), str)
            and is_private_login_process(row["CommandLine"])]
    pids = [pid for pid in pids if type(pid) is int and pid > 0]
    if not pids:
        return
    log(f"closing stale Chrome holding the private profile: pid {', '.join(map(str, pids))}")
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    time.sleep(2.0)
    # The next login replaces its debug-port file; leave it alone on kill failure.


def run_login(timeout: int) -> bool:
    if not SETUP_SCRIPT.exists():
        log(f"cannot renew: {SETUP_SCRIPT} is missing")
        return False
    if not debug_port_alive():
        close_stale_login_window()
    command = [python_executable(), "-B", str(SETUP_SCRIPT), "login", "--timeout", str(timeout)]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    log("token is due: running the auto-login script")
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            timeout=timeout + 90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"auto-login could not run: {type(exc).__name__}: {exc}")
        return False
    output = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    for line in output[-8:]:
        log(f"  login| {line}")
    if completed.returncode != 0:
        log(f"auto-login failed with exit code {completed.returncode}")
        return False

    # The login script only proves that a token was written; the gateway decides
    # whether it is usable. Verify that here, otherwise a renewal that produced a
    # token the gateway refuses (2026-10-25) looks like a success forever.
    state, detail = gateway_check(read_token())
    if state == "rejected":
        log(f"token rejected by the gateway right after login ({detail})")
        return False
    if state == "error":
        log(f"cannot confirm the fresh token yet: gateway unreachable ({detail})")
        return False
    return True


def proxy_healthy(port: int = PROXY_PORT, timeout: float = PROXY_HEALTH_TIMEOUT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            body = json.loads(response.read(65536).decode("utf-8", "replace") or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return body.get("service") == "um-opencode-proxy"


def ensure_proxy(port: int = PROXY_PORT) -> bool:
    """Start the local UM proxy when nothing answers on 8788.

    The DSH plugin now runs in always-on mode, but the host can be closed or
    restarting; without this the user's chat would 401/refuse until DSH happens
    to wake the proxy again.
    """
    if proxy_healthy(port):
        return True
    if not SETUP_SCRIPT.exists():
        log(f"proxy is down and {SETUP_SCRIPT} is missing; cannot start it")
        return False
    command = [
        python_executable(), "-B", str(SETUP_SCRIPT), "serve",
        "--port", str(port), "--ignore-console-events",
        "--log-file", str(HOME / "proxy.log"),
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(HOME),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            ),
        )
    except OSError as exc:
        log(f"proxy is down and could not be started: {type(exc).__name__}: {exc}")
        return False
    deadline = time.monotonic() + PROXY_START_TIMEOUT
    while time.monotonic() < deadline:
        if proxy_healthy(port):
            log(f"proxy was down; started it (pid {process.pid})")
            return True
        time.sleep(0.5)
    log(f"proxy was down; started pid {process.pid} but /health stayed silent for {int(PROXY_START_TIMEOUT)}s")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="print the token state and exit")
    parser.add_argument("--probe", action="store_true", help="ask the gateway whether the token is accepted")
    parser.add_argument("--force", action="store_true", help="run the auto-login even if the token looks fine")
    parser.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT_SECONDS, help="auto-login timeout in seconds")
    args = parser.parse_args()
    if args.timeout <= 0 or args.timeout > 600:
        parser.error("--timeout must be between 1 and 600 seconds")

    if not args.status and not args.probe:
        # Keep 8788 alive even while the DSH host is closed or restarting.
        ensure_proxy()

    now = time.time()
    exp = token_expiry()
    issued = token_issued()
    state = describe(exp, now)

    if args.status:
        print(f"token file : {TOKEN_FILE}")
        print(f"token state: {state}")
        if issued:
            print(f"issued at  : {local_time(issued)} ({int((now - issued) // 60)} min ago)")
        print(f"renew when : expired, less than {LEAD_SECONDS // 60} min left, older than "
              f"{MAX_TOKEN_AGE_SECONDS // 60} min, or the gateway rejects it")
        print(f"log file   : {LOG_FILE}")
        return 0

    if args.probe:
        probe_state, detail = gateway_check(read_token())
        print(f"token state: {state}")
        print(f"gateway    : {probe_state} ({detail})")
        return 0 if probe_state == "ok" else (2 if probe_state == "rejected" else 3)

    renewal_due = args.force or needs_renewal(exp, now, issued)
    if not renewal_due and issued is not None and now - issued >= MAX_TOKEN_AGE_SECONDS:
        renewal_due = True
    probe_state, probe_detail = ("unknown", "not probed")
    if not renewal_due:
        # exp says "fine", but the gateway can disagree. Trust the gateway.
        probe_state, probe_detail = gateway_check(read_token())
        if probe_state == "rejected":
            log(f"gateway rejected the token ({probe_detail}) even though the JWT says {state}")
            renewal_due = True

    if not renewal_due:
        if probe_state == "error":
            age = int((now - issued) // 60) if issued is not None else None
            if age is not None and now - issued >= PROBE_ERROR_AGE_SECONDS:
                log(
                    f"gateway probe unavailable ({probe_detail}) but the token is {age} min old; "
                    "renewing anyway"
                )
                renewal_due = True
            else:
                log(f"gateway probe unavailable ({probe_detail}); leaving token unchanged")
                return 3
        else:
            return 0

    wait = 0.0 if args.force else in_backoff(now)
    if wait > 0:
        log(f"token is due ({state}) but the last attempt failed; retrying in {int(wait // 60)} min")
        return 1

    if not acquire_lock(now):
        log(f"token is due ({state}) but another renewal is already running")
        return 1
    try:
        # Another renewal may have completed between the probe and this lock.
        if not args.force and not needs_renewal(token_expiry(), time.time(), token_issued()):
            current_state, _ = gateway_check(read_token())
            if current_state == "ok":
                return 0
        if probe_state == "unknown":
            probe_state, probe_detail = gateway_check(read_token())
        log(f"token is due: {state}; gateway {probe_state} ({probe_detail})")
        ok = run_login(args.timeout)
        if ok:
            fresh = token_expiry()
            log(f"auto-login finished: {describe(fresh, time.time())}")
            try:
                BACKOFF_FILE.unlink()
            except OSError:
                pass
            return 0
        log("auto-login did not produce a usable token; backing off before the next attempt")
        try:
            BACKOFF_FILE.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass
        return 1
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
