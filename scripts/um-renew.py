#!/usr/bin/env python
"""Keep the UM GenAI login token alive without waiting for a failed request.

The on-demand proxy only exists while it is being used, so the token watchdog it
runs itself cannot be relied on: while the proxy is asleep nothing notices that
the JWT expired, and the first request afterwards comes back 401 (the proxy does
try to renew inline, but that needs a browser window and often loses the race).

This watcher is independent of the proxy. Task Scheduler runs it every few
minutes; it reads the stored JWT and, only when the token is missing, expired or
about to expire, runs the auto-login script:

    C:\\Python314\\python.exe C:\\Users\\TINGOD\\.um-opencode\\um_opencode_setup.py login --timeout 120

Every decision and every outcome is appended to renew-watchdog.log. When the
token is still good the run costs one file read and exits quietly.

Usage:
    python scripts/um-renew.py --status    # show token state, change nothing
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

HOME = pathlib.Path(os.environ.get("UM_OPENCODE_HOME", pathlib.Path.home() / ".um-opencode"))
TOKEN_FILE = HOME / "upstream-token"
SETUP_SCRIPT = HOME / "um_opencode_setup.py"
LOG_FILE = HOME / "renew-watchdog.log"
LOCK_FILE = HOME / "renew-watchdog.lock"
BACKOFF_FILE = HOME / "renew-watchdog.last-fail"

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


def token_expiry() -> float | None:
    try:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    exp = data.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def local_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def describe(exp: float | None, now: float) -> str:
    if exp is None:
        return "no readable JWT in upstream-token"
    remaining = exp - now
    if remaining <= 0:
        return f"expired {local_time(exp)} ({int(-remaining // 60)} min ago)"
    return f"valid until {local_time(exp)} ({int(remaining // 60)} min left)"


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


def run_login(timeout: int) -> bool:
    if not SETUP_SCRIPT.exists():
        log(f"cannot renew: {SETUP_SCRIPT} is missing")
        return False
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
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="print the token state and exit")
    parser.add_argument("--force", action="store_true", help="run the auto-login even if the token looks fine")
    parser.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT_SECONDS, help="auto-login timeout in seconds")
    args = parser.parse_args()

    now = time.time()
    exp = token_expiry()
    state = describe(exp, now)

    if args.status:
        print(f"token file : {TOKEN_FILE}")
        print(f"token state: {state}")
        print(f"renew when : expired or less than {LEAD_SECONDS // 60} min left")
        print(f"log file   : {LOG_FILE}")
        return 0

    if not args.force and not needs_renewal(exp, now):
        return 0

    wait = 0.0 if args.force else in_backoff(now)
    if wait > 0:
        log(f"token is due ({state}) but the last attempt failed; retrying in {int(wait // 60)} min")
        return 1

    if not acquire_lock(now):
        log(f"token is due ({state}) but another renewal is already running")
        return 1
    try:
        log(f"token is due: {state}")
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
