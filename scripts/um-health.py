#!/usr/bin/env python3
"""One-shot health check for the UM GenAI proxy stack (UM 反代健康檢查).

Checks, in order:
  1. /health            - the proxy process is up and identifies itself
  2. /v1/models         - the local API key works end to end
  3. /diagnostics      - proxy counters, token expiry, recent upstream trouble
  4. token file         - when /diagnostics is unavailable (old proxy build)
  5. renew watchdog log - the last automatic token renewal
  6. scheduled tasks    - \\UMGenAIProxy-Renew and \\UMGenAIProxy-Models exist

Exit codes: 0 all good, 1 warnings (proxy asleep, token expiring, tasks unreadable),
2 failures (proxy answers but the stack is broken, token expired, models 401).

Run:
  python um-health.py
  python um-health.py --json
  python um-health.py --no-tasks          # skip schtasks (slow / restricted shell)
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOME = pathlib.Path.home()
DEFAULT_INSTALL = HOME / ".um-opencode"
WATCHDOG_LOG = DEFAULT_INSTALL / "renew-watchdog.log"
TASKS = (r"\UMGenAIProxy-Renew", r"\UMGenAIProxy-Models")

OK, WARN, FAIL = "ok", "warn", "fail"


class Checks:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.rows.append({"level": level, "name": name, "detail": detail})

    def worst(self) -> str:
        if any(row["level"] == FAIL for row in self.rows):
            return FAIL
        if any(row["level"] == WARN for row in self.rows):
            return WARN
        return OK


def http_json(url: str, token: str | None = None, timeout: float = 5.0):
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8", "replace"))


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def jwt_expiry(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = data.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def check_proxy(checks: Checks, base_url: str) -> None:
    try:
        with urllib.request.urlopen(f"{base_url.rsplit('/v1', 1)[0]}/health", timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        checks.add(WARN, "/health", f"proxy is asleep or down ({reason}) - it wakes on the next um-genai request")
        return
    service = payload.get("service", "")
    if service == "um-opencode-proxy":
        checks.add(OK, "/health", service)
    else:
        checks.add(FAIL, "/health", f"unexpected service field: {service!r}")


def check_models(checks: Checks, base_url: str, key_file: pathlib.Path) -> None:
    key = read_text(key_file)
    if not key:
        checks.add(FAIL, "local api key", f"{key_file} is empty or missing")
        return
    try:
        request = urllib.request.Request(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(request, timeout=20.0) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        level = FAIL if exc.code in (401, 403) else WARN
        checks.add(level, "/v1/models", f"HTTP {exc.code}")
        return
    except (urllib.error.URLError, OSError) as exc:
        checks.add(WARN, "/v1/models", f"not reachable ({getattr(exc, 'reason', exc)})")
        return
    models = payload.get("data") or []
    names = [item.get("id") for item in models if isinstance(item, dict)]
    detail = f"{len(names)} models"
    if names:
        detail += f" (e.g. {names[0]})"
    checks.add(OK if names else WARN, "/v1/models", detail)


def check_diagnostics(checks: Checks, base_url: str, key_file: pathlib.Path) -> bool:
    key = read_text(key_file)
    if not key:
        return False
    root = base_url.rsplit("/v1", 1)[0]
    try:
        status, payload = http_json(f"{root}/diagnostics?probe=1&log=1", token=key, timeout=30.0)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            checks.add(WARN, "/diagnostics", "not available on this proxy build - restart the proxy to enable it")
            return False
        checks.add(WARN, "/diagnostics", f"HTTP {exc.code}")
        return False
    except (urllib.error.URLError, OSError) as exc:
        checks.add(WARN, "/diagnostics", f"not reachable ({getattr(exc, 'reason', exc)})")
        return False

    proxy = payload.get("proxy") or {}
    stats = payload.get("stats") or {}
    checks.add(
        OK,
        "proxy stats",
        f"pid {proxy.get('pid')} up {int(proxy.get('uptimeSeconds') or 0)}s, "
        f"requests {stats.get('requests', 0)}, upstreamFailures {stats.get('upstreamFailures', 0)}, "
        f"retries {stats.get('upstreamRetries', 0)}",
    )

    token = payload.get("token") or {}
    seconds_left = token.get("secondsLeft")
    if token.get("present") and isinstance(seconds_left, (int, float)):
        minutes = int(seconds_left // 60)
        if seconds_left <= 0:
            checks.add(FAIL, "upstream token", f"expired {abs(minutes)} min ago")
        elif minutes < 30:
            checks.add(WARN, "upstream token", f"expires in {minutes} min ({token.get('expiresAt')})")
        else:
            checks.add(OK, "upstream token", f"valid for {minutes} min ({token.get('expiresAt')})")
    else:
        checks.add(FAIL, "upstream token", str(token.get("error") or "missing"))

    upstream = payload.get("upstream") or {}
    if upstream.get("ok"):
        checks.add(OK, "upstream reachability", f"UM gateway answered HTTP {upstream.get('status')}")
    else:
        checks.add(FAIL, "upstream reachability", str(upstream.get("error") or upstream.get("status")))

    log = payload.get("log") or {}
    if log.get("error"):
        checks.add(WARN, "proxy.log (24h)", str(log["error"]))
    else:
        worst = int(log.get("upstreamFailures") or 0)
        level = OK if worst == 0 else (WARN if worst < 5 else FAIL)
        detail = (
            f"{log.get('lines', 0)} lines, http5xx {log.get('http5xx', 0)}, "
            f"upstream failures {worst}, retries {log.get('upstreamRetries', 0)}"
        )
        if log.get("lastError"):
            detail += f"; last: {log['lastError'][:120]}"
        checks.add(level, "proxy.log (24h)", detail)
    return True


def check_token_file(checks: Checks, install_dir: pathlib.Path) -> None:
    token = read_text(install_dir / "upstream-token")
    expires = jwt_expiry(token) if token else None
    if expires is None:
        checks.add(WARN, "upstream-token file", "missing or not a JWT")
        return
    import time

    seconds_left = expires - time.time()
    minutes = int(abs(seconds_left) // 60)
    if seconds_left <= 0:
        checks.add(FAIL, "upstream-token file", f"expired {minutes} min ago")
    elif minutes < 30:
        checks.add(WARN, "upstream-token file", f"expires in {minutes} min")
    else:
        checks.add(OK, "upstream-token file", f"valid for {minutes} min")


def check_watchdog(checks: Checks, log_path: pathlib.Path) -> None:
    text = read_text(log_path)
    if not text:
        checks.add(WARN, "renew watchdog", f"{log_path} is empty or missing")
        return
    last = text.splitlines()[-1]
    if "auto-login succeeded" in last or "auto-login finished" in last:
        checks.add(OK, "renew watchdog", last[:160])
    elif "failed" in last.lower() or "skipped" in last.lower():
        checks.add(WARN, "renew watchdog", last[:160])
    else:
        checks.add(OK, "renew watchdog", last[:160])


def check_tasks(checks: Checks) -> None:
    for task in TASKS:
        try:
            completed = subprocess.run(
                ["schtasks", "/Query", "/TN", task, "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            checks.add(WARN, "scheduled tasks", f"cannot query schtasks ({type(exc).__name__})")
            return
        if completed.returncode != 0:
            checks.add(FAIL, f"task {task}", "not found (schtasks reported an error)")
            continue
        columns = [cell.strip().strip('"') for cell in completed.stdout.splitlines()[0].split(",")]
        status = columns[2] if len(columns) > 2 else "unknown"
        next_run = columns[1] if len(columns) > 1 else "unknown"
        level = OK if status.lower() in {"ready", "running", "disabled"} else WARN
        checks.add(level, f"task {task}", f"status {status}, next run {next_run}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Health check for the UM GenAI proxy stack.")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--base-url", default=None, help="defaults to http://127.0.0.1:<port>/v1")
    parser.add_argument(
        "--install-dir", type=pathlib.Path, default=DEFAULT_INSTALL, help="where token/api key files live"
    )
    parser.add_argument("--api-key-file", type=pathlib.Path, default=None)
    parser.add_argument("--json", action="store_true", help="machine readable output")
    parser.add_argument("--no-tasks", action="store_true", help="skip the scheduled-task checks")
    args = parser.parse_args(argv)

    base_url = args.base_url or f"http://127.0.0.1:{args.port}/v1"
    key_file = args.api_key_file or (args.install_dir / "local-api-key")

    checks = Checks()
    check_proxy(checks, base_url)
    check_models(checks, base_url, key_file)
    if not check_diagnostics(checks, base_url, key_file):
        check_token_file(checks, args.install_dir)
    check_watchdog(checks, args.install_dir / "renew-watchdog.log")
    if not args.no_tasks:
        check_tasks(checks)

    worst = checks.worst()
    if args.json:
        print(json.dumps({"verdict": worst, "checks": checks.rows}, ensure_ascii=False, indent=2))
    else:
        icons = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
        width = max(len(row["name"]) for row in checks.rows)
        for row in checks.rows:
            print(f"{icons[row['level']]}  {row['name'].ljust(width)}  {row['detail']}")
        print()
        summary = {OK: "all checks passed", WARN: "passed with warnings", FAIL: "FAILED"}
        print(f"{summary[worst]} ({worst})")

    return {OK: 0, WARN: 1, FAIL: 2}[worst]


if __name__ == "__main__":
    raise SystemExit(main())
