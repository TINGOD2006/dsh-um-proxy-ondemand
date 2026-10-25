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
LOGIN_TIMEOUT_SECONDS = 120
FAIL_BACKOFF_SECONDS = 15 * 60  # do not retry a failed login more often than this
STALE_LOCK_SECONDS = 15 * 60


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
    except OSError:
        return ""


def token_claims() -> dict:
    token = read_token()
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def token_expiry() -> float | None:
    exp = token_claims().get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def token_issued() -> float | None:
    iat = token_claims().get("iat")
    return float(iat) if isinstance(iat, (int, float)) else None


def local_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def describe(exp: float | None, now: float) -> str:
    if exp is None:
        return "no readable JWT in upstream-token"
    remaining = exp - now
    if remaining <= 0:
        return f"expired {local_time(exp)} ({int(-remaining // 60)} min ago)"
    return f"valid until {local_time(exp)} ({int(remaining // 60)} min left)"


def gateway_check(token: str) -> tuple[str, str]:
    """Ask the UM gateway whether it still accepts this token.

    Returns (state, detail) where state is "ok", "rejected" or "error".
    "error" means the network or gateway is unavailable: that is not the token's
    fault, so the caller must not trigger a renewal for it.
    """
    if not token:
        return "rejected", "no token stored"
    request = urllib.request.Request(f"{UPSTREAM_BASE}{PROBE_PATH}")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("User-Agent", "UMOpenCodeProxy/1.2")
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            return "ok", f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "rejected", f"HTTP {exc.code}"
        return "error", f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        return "error", f"{type(exc).__name__}: {exc}"


def needs_renewal(exp: float | None, now: float) -> bool:
    return exp is None or exp - now <= LEAD_SECONDS


def acquire_lock(now: float) -> bool:
    try:
        if LOCK_FILE.exists() and now - LOCK_FILE.stat().st_mtime > STALE_LOCK_SECONDS:
            LOCK_FILE.unlink()
    except OSError:
        pass
    try:
        descriptor = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return True  # cannot lock; better to try than to stay expired
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


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
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
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


def close_stale_login_window() -> None:
    """Kill a Chrome that holds the private login profile without a debug port.

    Chrome refuses to start a second instance on the same --user-data-dir: the new
    process only hands the URL to the old one and exits, so auto-login waits 90 s
    and reports "auto-login skipped: no login window with a debug port is open".
    The only fix is to close the squatter, which is a window we opened ourselves
    for a previous login attempt.
    """
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        "Where-Object { $_.CommandLine -like '*browser-profile*' -and "
        "$_.CommandLine -notlike '*--remote-debugging-port*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    output = powershell(script)
    pids = [line.strip() for line in output.splitlines() if line.strip().isdigit()]
    if not pids:
        return
    log(f"closing stale Chrome holding the private profile: pid {', '.join(pids)}")
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", pid, "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    time.sleep(2.0)
    try:
        DEBUG_PORT_FILE.unlink()
    except OSError:
        pass


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
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="print the token state and exit")
    parser.add_argument("--probe", action="store_true", help="ask the gateway whether the token is accepted")
    parser.add_argument("--force", action="store_true", help="run the auto-login even if the token looks fine")
    parser.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT_SECONDS, help="auto-login timeout in seconds")
    args = parser.parse_args()

    now = time.time()
    exp = token_expiry()
    issued = token_issued()
    state = describe(exp, now)

    if args.status:
        print(f"token file : {TOKEN_FILE}")
        print(f"token state: {state}")
        if issued:
            print(f"issued at  : {local_time(issued)} ({int((now - issued) // 60)} min ago)")
        print(f"renew when : expired, less than {LEAD_SECONDS // 60} min left, or the gateway rejects it")
        print(f"log file   : {LOG_FILE}")
        return 0

    if args.probe:
        probe_state, detail = gateway_check(read_token())
        print(f"token state: {state}")
        print(f"gateway    : {probe_state} ({detail})")
        return 0 if probe_state == "ok" else (2 if probe_state == "rejected" else 3)

    renewal_due = args.force or needs_renewal(exp, now)
    probe_state, probe_detail = ("unknown", "not probed")
    if not renewal_due:
        # exp says "fine", but the gateway can disagree. Trust the gateway.
        probe_state, probe_detail = gateway_check(read_token())
        if probe_state == "rejected":
            log(f"gateway rejected the token ({probe_detail}) even though the JWT says {state}")
            renewal_due = True

    if not renewal_due:
        return 0

    wait = 0.0 if args.force else in_backoff(now)
    if wait > 0:
        log(f"token is due ({state}) but the last attempt failed; retrying in {int(wait // 60)} min")
        return 1

    if not acquire_lock(now):
        log(f"token is due ({state}) but another renewal is already running")
        return 1
    try:
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
