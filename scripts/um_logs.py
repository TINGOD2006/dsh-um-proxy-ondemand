#!/usr/bin/env python
"""Keep the logs this toolchain writes small, and say when renewal is unhealthy.

Three files are written by three different processes and none of them agreed on
a retention rule:

  * renew-watchdog.log  - scripts/um-renew.py, one line per decision, no rotation
  * um-models-sync.log  - scripts/um-models-sync.py, only when it changes something
  * proxy.log           - um_opencode_setup.py serve, rotates itself at 8 MB

This module owns the rule for the first two and reports on all three. The
watchdog calls maintenance() once per cycle, so the cleanup needs no schedule of
its own; the settings page reads the snapshot it leaves behind.

Rotation keeps three backups (name.1 is the newest, name.3 the oldest) and only
runs when the live file is over 8 MB, so a quiet week costs nothing.

The health check answers one question: is renewal still working? It looks at the
last 24 hours of renew-watchdog.log for failed attempts and at the age of
renew-watchdog.last-success, which is the only honest clock - a re-login for the
same session returns the same JWT, so `iat` never advances.

Usage:
    python scripts/um_logs.py --json     # print the snapshot
    python scripts/um_logs.py --clean    # rotate now, then print the snapshot
    python scripts/um_logs.py --check    # print only the problems (exit 1 if any)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import tempfile
import time

HOME = pathlib.Path(os.environ.get("UM_OPENCODE_HOME", pathlib.Path.home() / ".um-opencode"))

MAX_BYTES = 8 * 1024 * 1024
BACKUPS = 3
HEALTH_FILE = HOME / "log-health.json"
LOG_FILE = HOME / "renew-watchdog.log"
LAST_SUCCESS_FILE = HOME / "renew-watchdog.last-success"

# name, path, rotate, label. proxy.log is left alone: the proxy rotates it on
# start, and renaming a file another process holds open fails on Windows.
LOGS = (
    ("renew-watchdog.log", LOG_FILE, True, "續期看門狗"),
    ("um-models-sync.log", HOME / "um-models-sync.log", True, "模型同步"),
    ("plugin.log", HOME / "plugin.log", True, "插件"),
    ("proxy.log", HOME / "proxy.log", False, "代理存取"),
)

# What a failed renewal looks like in renew-watchdog.log. Keep these in step
# with the log() calls in scripts/um-renew.py.
#
# Only the *terminal* line of a failed cycle counts. "auto-login failed with
# exit code N" fires once per attempt (hidden, then the visible fallback) and
# "retrying in N min" fires once per backoff cycle, so counting either would
# turn a single failure into four and trip the three-failure alarm on its own.
FAILURE_MARKERS = (
    "did not produce a usable token",
    "lock is unavailable",
    "auto-login could not run",
)
FAILURE_WINDOW_SECONDS = 24 * 60 * 60
FAILURE_LIMIT = 3
# The watchdog renews every 25 minutes, so a healthy last-success is younger
# than that; 30 minutes leaves one cycle of slack before it is worth a warning.
STALE_SUCCESS_SECONDS = 30 * 60
# A snapshot older than three watchdog cycles means the checker itself stopped.
# This is the one problem snapshot() cannot report about itself: it only writes
# while it runs, so a dead watchdog leaves a frozen file that still says
# "no problems". The judgement therefore belongs to the reader (settings page,
# um-health.py), which is what snapshot_age() is for.
SNAPSHOT_STALE_SECONDS = 15 * 60
TAIL_BYTES = 2 * 1024 * 1024
STAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def backup_path(path: pathlib.Path, index: int) -> pathlib.Path:
    return path.parent / f"{path.name}.{index}"


def backup_count(path: pathlib.Path, backups: int = BACKUPS) -> int:
    return sum(1 for index in range(1, backups + 1) if backup_path(path, index).exists())


def rotate(path: pathlib.Path, max_bytes: int | None = None, backups: int | None = None) -> dict:
    """Rename `path` to `path.1` once it is over `max_bytes`, keeping `backups` copies."""
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    backups = BACKUPS if backups is None else backups
    try:
        size = path.stat().st_size
    except OSError:
        return {"rotated": False, "reason": "missing", "bytes": 0}
    if size <= max_bytes:
        return {"rotated": False, "reason": "under-limit", "bytes": size}
    for attempt in range(2):
        try:
            oldest = backup_path(path, backups)
            if oldest.exists():
                oldest.unlink()
            for index in range(backups - 1, 0, -1):
                source = backup_path(path, index)
                if source.exists():
                    os.replace(source, backup_path(path, index + 1))
            os.replace(path, backup_path(path, 1))
        except OSError as exc:
            # Windows reports a sharing violation as PermissionError while the
            # writer still has the file open. plugin.log is written by the host
            # process, so one short retry covers that gap instead of reporting a
            # rotation failure that is really just a race.
            if attempt == 0:
                time.sleep(0.05)
                continue
            return {"rotated": False, "reason": f"error: {exc}", "bytes": size}
        return {"rotated": True, "reason": "over-limit", "bytes": size}
    return {"rotated": False, "reason": "error: unreachable", "bytes": size}


def count_lines(path: pathlib.Path) -> int:
    total = 0
    try:
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += chunk.count(b"\n")
    except OSError:
        return 0
    return total


def describe(path: pathlib.Path) -> dict:
    try:
        stat = path.stat()
    except OSError:
        return {
            "name": path.name,
            "path": str(path),
            "exists": False,
            "bytes": 0,
            "lines": 0,
            "modifiedAt": None,
            "backups": backup_count(path),
        }
    return {
        "name": path.name,
        "path": str(path),
        "exists": True,
        "bytes": stat.st_size,
        "lines": count_lines(path),
        "modifiedAt": int(stat.st_mtime),
        "backups": backup_count(path),
    }


def read_tail(path: pathlib.Path, limit: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def parse_stamp(line: str) -> float | None:
    match = STAMP.match(line)
    if not match:
        return None
    try:
        return time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def renewal_health(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    failures = 0
    last_failure = None
    last_failure_at = None
    for line in read_tail(LOG_FILE).splitlines():
        if not any(marker in line for marker in FAILURE_MARKERS):
            continue
        stamp = parse_stamp(line)
        if stamp is None or now - stamp > FAILURE_WINDOW_SECONDS:
            continue
        failures += 1
        last_failure = line.strip()
        last_failure_at = int(stamp)
    try:
        last_success = int(LAST_SUCCESS_FILE.stat().st_mtime)
    except OSError:
        last_success = None
    return {
        "failures24h": failures,
        "lastFailure": last_failure,
        "lastFailureAt": last_failure_at,
        "lastSuccessAt": last_success,
        "lastSuccessAgeSeconds": None if last_success is None else int(now - last_success),
    }


def check(now: float | None = None, rotations: list[dict] | None = None) -> list[dict]:
    now = time.time() if now is None else now
    problems: list[dict] = []
    health = renewal_health(now)
    if health["failures24h"] >= FAILURE_LIMIT:
        problems.append({
            "level": "warn",
            "code": "renewal-failures",
            "detail": f"最近 24 小時有 {health['failures24h']} 次續期失敗",
        })
    age = health["lastSuccessAgeSeconds"]
    if age is not None and age > STALE_SUCCESS_SECONDS:
        problems.append({
            "level": "warn",
            "code": "renewal-stale",
            "detail": f"距上次成功續期已 {age // 60} 分鐘",
        })
    for item in rotations or ():
        reason = str(item.get("reason", ""))
        if reason.startswith("error:"):
            problems.append({
                "level": "err",
                "code": "rotation-failed",
                "detail": f"{item.get('name', '日誌')} 輪替失敗：{reason[len('error:'):].strip()}",
            })
    return problems


def rotate_all() -> list[dict]:
    results = []
    for name, path, enabled, _label in LOGS:
        if not enabled:
            continue
        result = rotate(path)
        result["name"] = name
        results.append(result)
    return results


def snapshot(proxy_log: pathlib.Path | None = None, rotations: list[dict] | None = None,
             now: float | None = None) -> dict:
    now = time.time() if now is None else now
    files = []
    for name, path, enabled, label in LOGS:
        target = proxy_log if (name == "proxy.log" and proxy_log is not None) else path
        item = describe(target)
        # Keep the registry name as the identity: the proxy log can be pointed
        # somewhere else, and the settings page keys its rows on this.
        item["name"] = name
        item["label"] = label
        item["rotated"] = enabled
        files.append(item)
    return {
        "checkedAt": int(now),
        "staleAfterSeconds": SNAPSHOT_STALE_SECONDS,
        "maxBytes": MAX_BYTES,
        "backups": BACKUPS,
        "files": files,
        "renewal": renewal_health(now),
        "problems": check(now, rotations),
    }


def snapshot_age(health: dict | None, now: float | None = None) -> dict:
    """Judge how old a stored snapshot is.

    Returns ``{"ageSeconds": int | None, "stale": bool}``. A snapshot without a
    usable ``checkedAt`` counts as stale: freshness cannot be proven, and
    assuming it is fresh is exactly the failure this guards against.
    """
    now = time.time() if now is None else now
    health = health or {}
    try:
        checked = float(health.get("checkedAt"))
    except (TypeError, ValueError):
        return {"ageSeconds": None, "stale": True}
    age = max(0, int(now - checked))
    try:
        limit = float(health.get("staleAfterSeconds"))
    except (TypeError, ValueError):
        limit = float(SNAPSHOT_STALE_SECONDS)
    return {"ageSeconds": age, "stale": age > limit}


def write_atomic(path: pathlib.Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temp, path)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


def maintenance(proxy_log: pathlib.Path | None = None, now: float | None = None) -> dict:
    """Rotate what is due, then leave the snapshot the settings page reads."""
    rotations = rotate_all()
    data = snapshot(proxy_log=proxy_log, rotations=rotations, now=now)
    write_atomic(HEALTH_FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    parser.add_argument("--clean", action="store_true", help="rotate now, then print the snapshot")
    parser.add_argument("--check", action="store_true", help="print only the problems")
    parser.add_argument("--proxy-log", default=None, help="override the proxy log path")
    args = parser.parse_args(argv)
    proxy_log = pathlib.Path(args.proxy_log) if args.proxy_log else None
    data = maintenance(proxy_log=proxy_log) if args.clean else snapshot(proxy_log=proxy_log)
    if args.check:
        for problem in data["problems"]:
            print(f"{problem['level']}: {problem['detail']}")
        return 1 if data["problems"] else 0
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
