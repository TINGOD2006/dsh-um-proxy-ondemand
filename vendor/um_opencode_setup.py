#!/usr/bin/env python3
"""One-file classroom setup for using UM GenAI with OpenCode.

Python 3.10+ standard library only. The same file acts as installer, local
OpenAI-compatible proxy, OpenCode launcher, diagnostics, and uninstaller. On
macOS and Linux it monitors common browsers' local site storage; it does not
launch or control a browser and does not require Copy as cURL in the normal flow.

On Windows the token is read from a private browser profile that this script
opens once through the Chrome DevTools Protocol, because Windows browsers lock
their live site-storage files and compress them with Snappy.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import datetime as dt
import hashlib
import http.client
import json
import os
import pathlib
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


UPSTREAM = "https://chat.genai.um.edu.mo"
CHAT_HOST = urllib.parse.urlsplit(UPSTREAM).hostname or "chat.genai.um.edu.mo"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
INSTALL_DIR = pathlib.Path(
    os.environ.get("UM_OPENCODE_HOME") or (pathlib.Path.home() / ".um-opencode")
)
TOKEN_FILE = INSTALL_DIR / "upstream-token"
LOCAL_KEY_FILE = INSTALL_DIR / "local-api-key"
CONFIG_FILE = INSTALL_DIR / "opencode.json"
PID_FILE = INSTALL_DIR / "proxy.pid"
LOG_FILE = INSTALL_DIR / "proxy.log"
AUTO_REFRESH_SECONDS = float(os.environ.get("UM_AUTO_REFRESH_SECONDS", "300"))
SCRIPT_FILE = INSTALL_DIR / "um_opencode_setup.py"
BIN_DIR = INSTALL_DIR / "bin"
LAUNCHER = BIN_DIR / "um-opencode"
LAUNCHER_CMD = BIN_DIR / "um-opencode.cmd"
BROWSER_PROFILE_DIR = INSTALL_DIR / "browser-profile"
DEBUG_PORT_FILE = INSTALL_DIR / "browser-debug-port"

IS_WINDOWS = os.name == "nt"
# Console-subsystem children (icacls, tasklist, powershell, taskkill) open a fresh
# *visible* console window when the parent has none (e.g. the pythonw proxy), which
# shows up as a cmd/Terminal window flashing on a timer.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0

WINDOWS_BROWSER_EXES = (
    ("Chrome", "chrome.exe", ("Google/Chrome/Application",)),
    ("Edge", "msedge.exe", ("Microsoft/Edge/Application",)),
    ("Brave", "brave.exe", ("BraveSoftware/Brave-Browser/Application",)),
    ("Vivaldi", "vivaldi.exe", ("Vivaldi/Application",)),
    ("Chromium", "chromium.exe", ("Chromium/Application",)),
)

MODEL_CONFIG = {
    "DeepSeek-V4.1-Flash": {
        "name": "DeepSeek V4.1 Flash (UM - coding agent)",
        "tool_call": True,
        "temperature": True,
        "limit": {"context": 192768, "output": 32768},
    },
    "Qwen3.5-397B-A17B": {
        "name": "Qwen 3.5 397B A17B (UM - coding agent)",
        "tool_call": True,
        "temperature": True,
        "limit": {"context": 262144, "output": 32768},
    },
    "GLM-5.3-Flash": {
        "name": "GLM 5.3 Flash (UM)",
        "tool_call": True,
        "temperature": True,
        "limit": {"context": 131072, "output": 32768},
    },
    "Qwen3.8-27B": {
        "name": "Qwen 3.8 27B (UM)",
        "tool_call": True,
        "temperature": True,
        "limit": {"context": 131072, "output": 16384},
    },
    "gpt-oss-120b": {
        "name": "GPT-OSS 120B (UM)",
        "tool_call": True,
        "temperature": True,
        "limit": {"context": 32768, "output": 8192},
    },
    "gemma-4-31B-it": {
        "name": "Gemma 4 31B IT (UM - chat)",
        "tool_call": False,
        "temperature": True,
        "limit": {"context": 32768, "output": 8192},
    },
    "GLM-OCR": {
        "name": "GLM OCR (UM)",
        "attachment": True,
        "tool_call": False,
        "temperature": False,
        "limit": {"context": 8192, "output": 4096},
    },
}

# The deployment now answers OpenAI `tools` natively (verified directly against
# /api/chat/completions for DeepSeek-V4.1-Flash and Qwen3.5-397B-A17B), so the
# legacy DSML rewrite is disabled by default: the empty sentinel never matches a
# real model id. Set UM_DSML_MODEL=DeepSeek-V4-Flash only for an old deployment
# that still needs the bridge.
DEEPSEEK_NATIVE_MODEL = os.environ.get("UM_DSML_MODEL", "")
DSML_TOKEN = "｜DSML｜"
DSML_TOOLS_TEMPLATE = """## Tools

You have access to a set of tools to help answer the user's question. You can invoke tools by writing a "<{dsml}tool_calls>" block like the following:

<{dsml}tool_calls>
<{dsml}invoke name="$TOOL_NAME">
<{dsml}parameter name="$PARAMETER_NAME" string="true|false">$PARAMETER_VALUE</{dsml}parameter>
...
</{dsml}invoke>
<{dsml}invoke name="$TOOL_NAME2">
...
</{dsml}invoke>
</{dsml}tool_calls>

String parameters should be specified as is and set `string="true"`. For all other types (numbers, booleans, arrays, objects), pass the value in JSON format and set `string="false"`.

### Available Tool Schemas

{tool_schemas}

You MUST strictly follow the above defined tool name and parameter schemas to invoke tool calls."""


def die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def env_path(name: str, fallback: pathlib.Path) -> pathlib.Path:
    value = os.environ.get(name)
    return pathlib.Path(value) if value else fallback


def harden_windows_acl(path: pathlib.Path) -> None:
    """Best-effort equivalent of chmod 700 for the credential directory."""
    if not IS_WINDOWS:
        return
    account = os.environ.get("USERNAME")
    if not account:
        return
    domain = os.environ.get("USERDOMAIN")
    principal = f"{domain}\\{account}" if domain else account
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(OI)(CI)F"],
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def ensure_private_dir() -> None:
    INSTALL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    BIN_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    if IS_WINDOWS:
        os.chmod(INSTALL_DIR, 0o700)
        harden_windows_acl(INSTALL_DIR)
    else:
        os.chmod(INSTALL_DIR, 0o700)


def private_write(path: pathlib.Path, content: str) -> None:
    """Write bytes verbatim (no newline translation) with owner-only permissions."""
    ensure_private_dir()
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode("utf-8"))
        for attempt in range(12):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.25)
        os.chmod(path, 0o600)
    finally:
        try:
            temp.unlink()
        except (FileNotFoundError, PermissionError):
            pass


def read_secret(path: pathlib.Path, label: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        die(f"{label} is missing. Run: {SCRIPT_FILE} setup")
    if not value:
        die(f"{label} is empty. Run setup again.")
    return value


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        result = json.loads(base64.urlsafe_b64decode(encoded))
        return result if isinstance(result, dict) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def extract_token(text: str) -> str | None:
    patterns = (
        r"Authorization\s*:\s*Bearer\s+([^\s'\";]+)",
        r"Authorization:\s*Bearer\s+([^\s'\";]+)",
        r"['\"]Authorization['\"]\s*:\s*['\"]Bearer\s+([^'\"]+)",
        r"(?:^|\s)-H\s+['\"]Authorization:\s*Bearer\s+([^'\"]+)['\"]",
        r"(?:^|\s)--header\s+['\"]Authorization:\s*Bearer\s+([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            candidate = match.group(1).strip().rstrip("'\";\\^")
            if len(candidate) >= 12:
                return candidate
    stripped = text.strip()
    if stripped.lower().startswith("bearer "):
        stripped = stripped[7:].strip()
    if re.fullmatch(r"[A-Za-z0-9._~+/-]{12,}", stripped):
        return stripped
    return None


JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)


def chromium_profile_roots(
    home: pathlib.Path | None = None, platform_name: str | None = None
) -> list[tuple[str, pathlib.Path]]:
    home = pathlib.Path.home() if home is None else home
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name == "win32":
        local = env_path("LOCALAPPDATA", home / "AppData/Local")
        roaming = env_path("APPDATA", home / "AppData/Roaming")
        return [
            ("Google Chrome", local / "Google/Chrome/User Data"),
            ("Google Chrome Beta", local / "Google/Chrome Beta/User Data"),
            ("Google Chrome Dev", local / "Google/Chrome Dev/User Data"),
            ("Google Chrome Canary", local / "Google/Chrome SxS/User Data"),
            ("Microsoft Edge", local / "Microsoft/Edge/User Data"),
            ("Microsoft Edge Beta", local / "Microsoft/Edge Beta/User Data"),
            ("Microsoft Edge Dev", local / "Microsoft/Edge Dev/User Data"),
            ("Brave", local / "BraveSoftware/Brave-Browser/User Data"),
            ("Chromium", local / "Chromium/User Data"),
            ("Vivaldi", local / "Vivaldi/User Data"),
            ("Opera", roaming / "Opera Software/Opera Stable"),
            ("Opera GX", roaming / "Opera Software/Opera GX Stable"),
        ]
    if platform_name.startswith("linux"):
        config = home / ".config"
        flatpak = home / ".var/app"
        return [
            ("Google Chrome", config / "google-chrome"),
            ("Google Chrome Beta", config / "google-chrome-beta"),
            ("Google Chrome", flatpak / "com.google.Chrome/config/google-chrome"),
            ("Chromium", config / "chromium"),
            ("Chromium", home / "snap/chromium/common/chromium"),
            ("Chromium", flatpak / "org.chromium.Chromium/config/chromium"),
            ("Microsoft Edge", config / "microsoft-edge"),
            ("Microsoft Edge Beta", config / "microsoft-edge-beta"),
            ("Microsoft Edge Dev", config / "microsoft-edge-dev"),
            ("Microsoft Edge", flatpak / "com.microsoft.Edge/config/microsoft-edge"),
            ("Brave", config / "BraveSoftware/Brave-Browser"),
            ("Brave", flatpak / "com.brave.Browser/config/BraveSoftware/Brave-Browser"),
            ("Vivaldi", config / "vivaldi"),
            ("Vivaldi", flatpak / "com.vivaldi.Vivaldi/config/vivaldi"),
            ("Opera", config / "opera"),
            ("Opera", home / "snap/opera/current/.config/opera"),
        ]
    support = home / "Library/Application Support"
    return [
        ("Google Chrome", support / "Google/Chrome"),
        ("Microsoft Edge", support / "Microsoft Edge"),
        ("Brave", support / "BraveSoftware/Brave-Browser"),
        ("Arc", support / "Arc/User Data"),
        ("Vivaldi", support / "Vivaldi"),
        ("Opera", support / "com.operasoftware.Opera"),
        ("Opera GX", support / "com.operasoftware.OperaGX"),
        ("Chromium", support / "Chromium"),
    ]


def firefox_profile_roots(
    home: pathlib.Path | None = None, platform_name: str | None = None
) -> list[tuple[str, pathlib.Path]]:
    home = pathlib.Path.home() if home is None else home
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name == "win32":
        roaming = env_path("APPDATA", home / "AppData/Roaming")
        return [
            ("Firefox", roaming / "Mozilla/Firefox/Profiles"),
            ("Firefox ESR", roaming / "Mozilla/Firefox ESR/Profiles"),
            ("Zen", roaming / "zen/Profiles"),
            ("Floorp", roaming / "Floorp/Profiles"),
            ("Waterfox", roaming / "Waterfox/Profiles"),
            ("LibreWolf", roaming / "librewolf/Profiles"),
        ]
    if platform_name.startswith("linux"):
        flatpak = home / ".var/app"
        return [
            ("Firefox", home / ".mozilla/firefox"),
            ("Firefox (Snap)", home / "snap/firefox/common/.mozilla/firefox"),
            ("Firefox (Flatpak)", flatpak / "org.mozilla.firefox/.mozilla/firefox"),
            ("Firefox ESR", home / ".mozilla/firefox-esr"),
            ("Zen", home / ".zen"),
            ("Zen (Flatpak)", flatpak / "app.zen_browser.zen/.zen"),
            ("Floorp", home / ".floorp"),
            ("Floorp (Flatpak)", flatpak / "one.ablaze.floorp/.floorp"),
            ("Waterfox", home / ".waterfox"),
        ]
    support = home / "Library/Application Support"
    return [
        ("Firefox", support / "Firefox/Profiles"),
        ("Firefox ESR", support / "Firefox ESR/Profiles"),
        ("Zen", support / "zen/Profiles"),
        ("Floorp", support / "Floorp/Profiles"),
        ("Waterfox", support / "Waterfox/Profiles"),
    ]


def browser_storage_files(
    home: pathlib.Path | None = None, platform_name: str | None = None
) -> list[tuple[str, pathlib.Path]]:
    """Find token-bearing storage files in common macOS and Linux browser profiles."""
    home = pathlib.Path.home() if home is None else home
    platform_name = sys.platform if platform_name is None else platform_name
    results: list[tuple[str, pathlib.Path]] = []
    for browser, user_data in chromium_profile_roots(home, platform_name):
        if not user_data.is_dir():
            continue
        profiles = [user_data]
        try:
            profiles.extend(
                path
                for path in user_data.iterdir()
                if path.is_dir() and (path.name == "Default" or path.name.startswith("Profile "))
            )
        except OSError:
            pass
        for profile in profiles:
            locations = [
                profile / "Local Storage/leveldb",
                profile / "Session Storage",
                profile / "IndexedDB/https_chat.genai.um.edu.mo_0.indexeddb.leveldb",
            ]
            for location in locations:
                if not location.is_dir():
                    continue
                try:
                    children = list(location.iterdir())
                except OSError:
                    continue
                for path in children:
                    if path.is_file() and path.suffix.lower() in {".log", ".ldb", ".sqlite"}:
                        results.append((browser, path))
    for browser, profiles_root in firefox_profile_roots(home, platform_name):
        if not profiles_root.is_dir():
            continue
        try:
            profiles = list(profiles_root.iterdir())
        except OSError:
            continue
        for profile in profiles:
            storage_root = profile / "storage/default"
            if not storage_root.is_dir():
                continue
            try:
                origins = list(storage_root.glob("https+++chat.genai.um.edu.mo*"))
            except OSError:
                continue
            for origin in origins:
                try:
                    files = list(origin.rglob("*"))
                except OSError:
                    continue
                for path in files:
                    if path.is_file() and path.suffix.lower() in {".sqlite", ".log", ".ldb"}:
                        results.append((browser, path))
    if platform_name == "darwin":
        safari_locations = [
            home / "Library/Safari/LocalStorage",
            home / "Library/Containers/com.apple.Safari/Data/Library/Safari/LocalStorage",
            home
            / "Library/Containers/com.apple.Safari/Data/Library/WebKit/WebsiteData/LocalStorage",
        ]
        for location in safari_locations:
            if not location.is_dir():
                continue
            try:
                files = list(location.rglob("*"))
            except OSError:
                continue
            for path in files:
                if path.is_file() and path.suffix.lower() in {
                    ".localstorage",
                    ".sqlite",
                    ".log",
                    ".ldb",
                }:
                    results.append(("Safari", path))
    unique: dict[pathlib.Path, str] = {}
    for browser, path in results:
        unique[path] = browser
    return [(browser, path) for path, browser in unique.items()]


def extract_browser_token_candidates(data: bytes) -> list[str]:
    candidates: list[str] = []
    decoded_variants = [data.decode("latin-1", "ignore")]
    if len(data) >= 2:
        decoded_variants.extend(
            (
                data.decode("utf-16le", "ignore"),
                data[1:].decode("utf-16le", "ignore"),
            )
        )
    for decoded in decoded_variants:
        for token in JWT_PATTERN.findall(decoded):
            payload = decode_jwt_payload(token)
            if payload and {"id", "iat", "jti"}.issubset(payload):
                candidates.append(token)
    return list(dict.fromkeys(candidates))


def probe_um_token(token: str) -> bool | None:
    """True=valid UM token, False=rejected, None=temporary network/server error."""
    try:
        payload = request_json(f"{UPSTREAM}/api/models", token=token, timeout=12)
    except urllib.error.HTTPError as exc:
        return False if exc.code in {401, 403} else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        models = payload
    elif isinstance(payload, dict):
        models = payload.get("data") or payload.get("models") or []
    else:
        models = []
    return bool(models)


def locate_browser_executable(exe_name: str, subdirectories: tuple[str, ...]) -> pathlib.Path | None:
    """Find a Chromium-family browser on Windows, registry App Paths first."""
    candidates: list[pathlib.Path] = []
    if IS_WINDOWS:
        try:
            import winreg

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\\" + exe_name,
                    ) as key:
                        value, _kind = winreg.QueryValueEx(key, None)
                        if value:
                            candidates.append(pathlib.Path(value))
                except OSError:
                    continue
        except ImportError:
            pass
        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        for root in roots:
            if not root:
                continue
            for subdirectory in subdirectories:
                candidates.append(pathlib.Path(root) / subdirectory / exe_name)
    else:
        for name in ("google-chrome", "chromium", "microsoft-edge", "brave-browser", "vivaldi"):
            found = shutil.which(name)
            if found:
                candidates.append(pathlib.Path(found))
        mac_bundle = pathlib.Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        candidates.append(mac_bundle)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def find_debug_browser(preferred: str | None = None) -> tuple[str, pathlib.Path] | None:
    wanted = (preferred or "auto").lower()
    entries = list(WINDOWS_BROWSER_EXES)
    if wanted != "auto":
        entries.sort(key=lambda item: 0 if item[0].lower() == wanted else 1)
    for name, exe_name, subdirectories in entries:
        if wanted != "auto" and name.lower() != wanted:
            continue
        located = locate_browser_executable(exe_name, subdirectories)
        if located:
            return name, located
    return None


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((DEFAULT_HOST, 0))
        return int(probe.getsockname()[1])


class WebSocketError(RuntimeError):
    """Raised when the Chrome DevTools WebSocket session fails."""


class DevToolsSocket:
    """Minimal RFC 6455 text client: enough to drive the Chrome DevTools Protocol."""

    def __init__(self, url: str, timeout: float = 15.0) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise WebSocketError(f"Unsupported WebSocket URL: {url}")
        host = parsed.hostname or DEFAULT_HOST
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        raw = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        raw.settimeout(timeout)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("DevTools closed the connection during the handshake")
            header += chunk
            if len(header) > 65536:
                raise WebSocketError("DevTools handshake response is too large")
        status = header.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise WebSocketError(f"DevTools rejected the WebSocket handshake: {status!r}")

    def _read_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise WebSocketError("DevTools closed the connection")
            data += chunk
        return data

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        frame = bytearray([0x80 | opcode])
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame += struct.pack(">H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", length)
        frame += mask
        frame += bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(frame))

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def receive_text(self) -> str | None:
        """Return the next text message, or None when the peer closes the socket."""
        buffer = bytearray()
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if masked else b""
            payload = self._read_exact(length) if length else b""
            if masked:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x0}:
                buffer += payload
                if final:
                    return buffer.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class DevToolsSession:
    """Request/response helper on top of DevToolsSocket."""

    def __init__(self, url: str, timeout: float = 15.0) -> None:
        self.socket = DevToolsSocket(url, timeout)
        self._next_id = 0

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        self.socket.send_text(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebSocketError(f"DevTools did not answer {method} in time")
            self.socket.sock.settimeout(max(0.5, min(remaining, 5.0)))
            text = self.socket.receive_text()
            if text is None:
                raise WebSocketError("DevTools closed the connection")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("id") == message_id:
                if "error" in payload:
                    raise WebSocketError(str(payload["error"]))
                result = payload.get("result")
                return result if isinstance(result, dict) else {}

    def close(self) -> None:
        self.socket.close()


LOCAL_STORAGE_PROBE = (
    "(function(){var out=[];"
    "try{for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);"
    "var v=localStorage.getItem(k);if(v){out.push(k+'='+v);}}}catch(e){}"
    "try{for(var i=0;i<sessionStorage.length;i++){var k=sessionStorage.key(i);"
    "var v=sessionStorage.getItem(k);if(v){out.push(k+'='+v);}}}catch(e){}"
    "try{out.push('cookie='+document.cookie);}catch(e){}"
    "return out.join('\\n');})()"
)


def devtools_targets(port: int, timeout: float = 5.0) -> list[dict[str, Any]]:
    try:
        payload = request_json(f"http://{DEFAULT_HOST}:{port}/json/list", timeout=timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def chat_page_target(port: int) -> dict[str, Any] | None:
    for item in devtools_targets(port):
        if item.get("type") != "page" or not item.get("webSocketDebuggerUrl"):
            continue
        if "chat.genai.um.edu.mo" in str(item.get("url", "")):
            return item
    return None


def existing_debug_port(any_page: bool = False) -> int | None:
    """Debug port of a login window this script opened earlier, when still usable.

    With *any_page* a window still sitting on the school SSO page counts too:
    starting a second Chrome against the same private profile would only hand
    the URL over to the running one and never expose a debug port, so callers
    must attach to that window instead of launching a competitor.
    """
    try:
        port = int(DEBUG_PORT_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None
    if any_page:
        return port if devtools_targets(port) else None
    return port if chat_page_target(port) else None


def launch_debug_browser(port: int, *, headless: bool) -> subprocess.Popen[bytes] | None:
    """Start Chrome/Edge against the private UM profile with a DevTools port.

    *headless* is for the automatic sign-in: there is nothing for a human to look
    at, and a window that is opened anyway stays on screen forever. The visible
    variant exists only for the by-hand fallback, where someone has to approve a
    two-factor prompt.
    """
    found = find_debug_browser(None)
    if not found:
        return None
    command = [
        str(found[1]),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={BROWSER_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
    ]
    if headless:
        command.extend(["--headless=new", "--disable-gpu"])
    else:
        command.extend(["--new-window", "--start-maximized"])
    command.append(f"{UPSTREAM}/")
    creation_flags = 0
    if IS_WINDOWS:
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            close_fds=True,
        )
    except OSError:
        return None


def capture_token_via_devtools(
    timeout: int, *, browser: str | None = None, debug_port: int | None = None
) -> str:
    """Open a private browser profile, watch the login, and read the UM session token."""
    found = find_debug_browser(browser)
    if not found:
        die(
            "No Chrome/Edge installation was found, so the assisted login window cannot "
            "be opened. Use the --curl-file fallback from STUDENT_GUIDE.md instead."
        )
    browser_name, executable = found
    reuse = existing_debug_port()
    port = debug_port or reuse or free_local_port()
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(
        f"Opening a private {browser_name} window for the one-time UM login.\n"
        "  1. Log in at https://chat.genai.um.edu.mo/ in the window that just opened.\n"
        "  2. Send any short chat message.\n"
        "This window uses its own profile, so your normal browser profile is untouched:\n"
        f"  {BROWSER_PROFILE_DIR}\n"
        "Only the Open WebUI session token is read. Your password is never read or stored."
    )
    if reuse and reuse == port:
        print(f"Reusing the {browser_name} login window that is already open (port {port}).")
        launched_at = None
    else:
        creation_flags = 0
        if IS_WINDOWS:
            creation_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        try:
            subprocess.Popen(
                [
                    str(executable),
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={BROWSER_PROFILE_DIR}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-session-crashed-bubble",
                    "https://chat.genai.um.edu.mo/",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
                close_fds=True,
            )
        except OSError as exc:
            die(f"Could not start {browser_name}: {exc}")
        private_write(DEBUG_PORT_FILE, f"{port}\n")
        launched_at = time.monotonic()

    deadline = time.monotonic() + timeout
    rejected: set[str] = set()
    session: DevToolsSession | None = None
    announced = False
    warned = False
    last_notice = time.monotonic()
    while time.monotonic() < deadline:
        if session is None:
            target = chat_page_target(port)
            if target is None:
                if (
                    launched_at is not None
                    and not warned
                    and time.monotonic() - launched_at > 25
                ):
                    warned = True
                    print(
                        f"No debug window answered on port {port}. If a {browser_name} "
                        "window from an earlier run is still open with the same private "
                        "profile, close that window and run this command again."
                    )
                time.sleep(1.0)
                continue
            try:
                session = DevToolsSession(str(target["webSocketDebuggerUrl"]), timeout=15.0)
            except (OSError, WebSocketError):
                session = None
                time.sleep(1.0)
                continue
            if not announced:
                print("Connected to the login window; watching for the UM session token...")
                announced = True
        try:
            result = session.call(
                "Runtime.evaluate",
                {
                    "expression": LOCAL_STORAGE_PROBE,
                    "returnByValue": True,
                    "awaitPromise": False,
                },
            )
        except (OSError, WebSocketError):
            session.close()
            session = None
            time.sleep(1.0)
            continue
        value = result.get("result", {}).get("value") if isinstance(result, dict) else None
        if isinstance(value, str):
            for token in extract_browser_token_candidates(value.encode("utf-8", "replace")):
                digest = hashlib.sha256(token.encode()).hexdigest()
                if digest in rejected:
                    continue
                if probe_um_token(token) is True:
                    print(f"Authenticated UM session detected in {browser_name}.")
                    session.close()
                    return token
                rejected.add(digest)
        if time.monotonic() - last_notice > 20:
            remaining = max(0, int(deadline - time.monotonic()))
            print(f"Still waiting for the UM login... ({remaining}s left)")
            last_notice = time.monotonic()
        time.sleep(1.5)
    if session is not None:
        session.close()
    die(
        "Timed out without finding a valid UM session. Run setup again, or use the "
        "--curl-file fallback described in STUDENT_GUIDE.md."
    )


def stop_process_tree(pid: int) -> None:
    """Best-effort termination of a browser this script started itself."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        pass


def silent_token_capture(
    timeout: float = 90.0,
    *,
    prefer_existing: bool = True,
    headless: bool = False,
) -> str | None:
    """Re-capture the UM token without asking the user for anything.

    Reuses a login window this tool already has open when one is reachable;
    otherwise it starts Chromium against the same private profile, which
    recreates the UM session from the stored cookie. Never raises and never
    exits, so the background watchdog cannot take the proxy down with it.
    Returns None when no valid token could be obtained.
    """
    found = find_debug_browser(None)
    if not found:
        return None
    _, executable = found
    port = existing_debug_port(any_page=True) if prefer_existing else None
    launched: subprocess.Popen[bytes] | None = None
    if port is None:
        port = free_local_port()
        creation_flags = 0
        if IS_WINDOWS:
            creation_flags = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        command = [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={BROWSER_PROFILE_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
        ]
        if headless:
            command.extend(["--headless=new", "--disable-gpu"])
        command.append("https://chat.genai.um.edu.mo/")
        try:
            launched = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
                close_fds=True,
            )
        except OSError:
            return None
        try:
            private_write(DEBUG_PORT_FILE, f"{port}\n")
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    rejected: set[str] = set()
    session: DevToolsSession | None = None
    try:
        while time.monotonic() < deadline:
            if session is None and launched is not None and launched.poll() is not None:
                # Chrome handed the URL to an instance that already owns this profile,
                # so no debug port of ours will ever open: fail fast, do not wait.
                return None
            if session is None:
                target = chat_page_target(port)
                if target is None:
                    time.sleep(0.75)
                    continue
                try:
                    session = DevToolsSession(str(target["webSocketDebuggerUrl"]), timeout=15.0)
                except (OSError, WebSocketError):
                    session = None
                    time.sleep(0.75)
                    continue
            try:
                result = session.call(
                    "Runtime.evaluate",
                    {
                        "expression": LOCAL_STORAGE_PROBE,
                        "returnByValue": True,
                        "awaitPromise": False,
                    },
                )
            except (OSError, WebSocketError):
                session.close()
                session = None
                time.sleep(0.75)
                continue
            value = result.get("result", {}).get("value") if isinstance(result, dict) else None
            if isinstance(value, str):
                for token in extract_browser_token_candidates(value.encode("utf-8", "replace")):
                    digest = hashlib.sha256(token.encode()).hexdigest()
                    if digest in rejected:
                        continue
                    if probe_um_token(token) is True:
                        return token
                    rejected.add(digest)
            time.sleep(0.75)
    finally:
        if session is not None:
            session.close()
        if launched is not None:
            stop_process_tree(launched.pid)
    return None


def token_watchdog(interval: float) -> None:
    """Keep the UM token fresh so an expired session never reaches the user."""
    delay = 20.0  # check soon after startup, then settle into the normal rhythm
    while True:
        time.sleep(delay)
        delay = interval
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not token:
            continue
        try:
            state = probe_um_token(token)
        except Exception:  # noqa: BLE001 - a watchdog must never die
            continue
        if state is not False:
            continue
        stamp = time.strftime("%d/%b/%Y %H:%M:%S")
        print(f"[{stamp}] UM token was rejected; trying a silent refresh...", flush=True)
        try:
            renewed = renew_token_once(cooldown=240.0)
        except Exception:  # noqa: BLE001 - a watchdog must never die
            renewed = False
        stamp = time.strftime("%d/%b/%Y %H:%M:%S")
        if renewed:
            print(f"[{stamp}] token refreshed automatically.", flush=True)
        else:
            print(
                f"[{stamp}] automatic re-login failed (no stored credentials, a rejected "
                "password, or a two-factor prompt nobody approved). Opening the login window "
                "so you can sign in by hand.",
                flush=True,
            )
            try:
                open_login_window()
            except Exception:  # noqa: BLE001 - a watchdog must never die
                pass


_TOKEN_LOCK = threading.Lock()
_last_renew_attempt = 0.0
_last_login_prompt = 0.0
RENEW_COOLDOWN = 60.0
LOGIN_PROMPT_COOLDOWN = 600.0
SESSION_EXPIRED_MESSAGE = (
    "UM session expired and could not be renewed automatically. A private login window "
    "was opened: sign in at https://chat.genai.um.edu.mo/ there and the proxy recovers "
    f'by itself. Manual alternative: python "{SCRIPT_FILE}" refresh'
)


def current_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def renew_token_once(cooldown: float = RENEW_COOLDOWN) -> bool:
    """Renew the UM token at most once per cooldown.

    Returns True when a valid token is in place, so a caller that arrived while
    another thread was already renewing can simply retry its request.
    """
    global _last_renew_attempt
    with _TOKEN_LOCK:
        now = time.monotonic()
        if now - _last_renew_attempt < cooldown:
            return probe_um_token(current_token()) is True
        _last_renew_attempt = now
    fresh = silent_token_capture(headless=True, timeout=25.0)
    if not fresh:
        # The private profile lost its UM cookies: sign in again with the stored
        # credentials (this is where a Duo approval may need a human).
        fresh = auto_login()
    if not fresh:
        return False
    private_write(TOKEN_FILE, fresh + "\n")
    print(f"[{time.strftime('%d/%b/%Y %H:%M:%S')}] token renewed on demand.", flush=True)
    return True


def open_login_window() -> None:
    """Open the assisted login window once when nothing else can renew the session."""
    global _last_login_prompt
    with _TOKEN_LOCK:
        now = time.monotonic()
        if now - _last_login_prompt < LOGIN_PROMPT_COOLDOWN:
            return
        _last_login_prompt = now
    if existing_debug_port(any_page=True):
        return
    if find_debug_browser(None) is None:
        return
    port = free_local_port()
    process = launch_debug_browser(port, headless=False)
    if process is None:
        return
    private_write(DEBUG_PORT_FILE, f"{port}\n")
    stamp = time.strftime("%d/%b/%Y %H:%M:%S")
    # Confirm the window really came up: a launch that silently handed the URL to
    # an instance already owning the profile must not look like a success, or the
    # user is left waiting for a window that never appears.
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and process.poll() is None:
        if devtools_targets(port):
            break
        time.sleep(0.4)
    if not devtools_targets(port):
        with _TOKEN_LOCK:
            _last_login_prompt = 0.0
        print(
            f"[{stamp}] could not open the UM login window automatically "
            "(no debug port answered). Run 'refresh' to log in by hand.",
            flush=True,
        )
        return
    print(
        f"[{stamp}] the UM session cannot be renewed silently; opened the login window "
        "- sign in there once and the proxy recovers by itself.",
        flush=True,
    )


CREDENTIALS_FILE = INSTALL_DIR / "credentials.dat"
AUTO_LOGIN_SECONDS = float(os.environ.get("UM_AUTO_LOGIN_SECONDS", "90"))

AUTOLOGIN_STATE_JS = """
(function(){
  var out = {href: location.href, ready: document.readyState};
  out.hasPassword = !!document.querySelector('input[type=password]');
  out.error = '';
  var t = document.body ? document.body.innerText : '';
  var m = t.match(/(incorrect|invalid|不正確|錯誤|失敗|逾時|locked|expired)[^\\n]{0,60}/i);
  if (m) out.error = m[0];
  out.token = '';
  try { out.token = localStorage.getItem('token') || ''; } catch (e) {}
  return JSON.stringify(out);
})()
"""

AUTOLOGIN_UMPASS_JS = """
(function(){
  var b = Array.prototype.slice.call(document.querySelectorAll('button')).filter(function(el){
    return /UMPASS|SSO 繼續|Continue with/i.test(el.innerText || '');
  })[0];
  if (!b) return 'no-button';
  b.click();
  return 'clicked';
})()
"""

AUTOLOGIN_FILL_JS = """
(function(u, p){
  function set(el, value){
    var proto = Object.getPrototypeOf(el);
    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('keyup', {bubbles: true}));
  }
  var uEl = document.querySelector('#userNameInput') || document.querySelector('input[type=email]')
         || document.querySelector('input[type=text]');
  var pEl = document.querySelector('#passwordInput') || document.querySelector('input[type=password]');
  if (!uEl || !pEl) return 'missing';
  set(uEl, u); set(pEl, p);
  var kmsi = document.querySelector('#kmsiInput');
  if (kmsi && !kmsi.checked) { kmsi.checked = true; kmsi.dispatchEvent(new Event('change', {bubbles: true})); }
  return JSON.stringify({ok: true, user_len: uEl.value.length, pass_len: pEl.value.length});
})(%s, %s)
"""

AUTOLOGIN_SUBMIT_JS = """
(function(){
  var p = document.querySelector('input[type=password]');
  var f = p ? p.closest('form') : null;
  if (!f) return 'no-password-form';
  var el = f.querySelector('#submitButton, input[type=submit], button[type=submit], button');
  if (el && el.tagName !== 'INPUT' && el.tagName !== 'BUTTON') {
    return JSON.stringify({kind: 'control', id: el.id || ''});
  }
  try {
    if (el) { f.requestSubmit(el); return 'requestSubmit'; }
    if (f.requestSubmit) { f.requestSubmit(); return 'requestSubmit()'; }
    f.submit(); return 'submit()';
  } catch (e) { return 'error:' + e.message; }
})()
"""

AUTOLOGIN_CLICK_JS = """
(function(id){
  var el = id ? document.getElementById(id) : null;
  if (!el) return 'missing';
  el.click();
  return 'clicked';
})(%s)
"""

AUTOLOGIN_DUO_PUSH_JS = """
(function(){
  var wanted = /push|傳送|发送|發送|send me|呼叫|call me/i;
  var els = Array.prototype.slice.call(document.querySelectorAll('button, a, div[role=button]'));
  var hit = els.filter(function(el){ return wanted.test(el.innerText || ''); })[0];
  if (!hit) return 'no-push-control';
  hit.click();
  return 'pushed';
})()
"""


def _dpapi_crypt(blob: bytes, *, unprotect: bool = False) -> bytes:
    """Encrypt/decrypt with the current Windows user's DPAPI key (user-scoped)."""
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(blob, len(blob))
    source = DataBlob(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = DataBlob()
    crypt32 = ctypes.windll.crypt32
    call = crypt32.CryptUnprotectData if unprotect else crypt32.CryptProtectData
    if not call(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def save_credentials(username: str, password: str) -> None:
    """Store the UM username/password, encrypted with DPAPI on Windows.

    Plain text never touches the disk: on Windows the blob is wrapped with
    CryptProtectData (readable only by this Windows user), elsewhere it is
    base64 (with a warning, since the tool's other platforms have no keyring).
    """
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    if IS_WINDOWS:
        envelope = "dpapi:" + base64.b64encode(_dpapi_crypt(payload)).decode("ascii")
    else:
        envelope = "plain:" + base64.b64encode(payload).decode("ascii")
    private_write(CREDENTIALS_FILE, envelope)
    print(f"stored UM credentials for {username!r} in {CREDENTIALS_FILE}")


def load_credentials() -> tuple[str, str] | None:
    try:
        envelope = CREDENTIALS_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        scheme, _, blob = envelope.partition(":")
        raw = base64.b64decode(blob)
        data = json.loads((_dpapi_crypt(raw, unprotect=True) if scheme == "dpapi" else raw).decode("utf-8"))
        return str(data["username"]), str(data["password"])
    except Exception:
        print(f"stored credentials in {CREDENTIALS_FILE} could not be read; run 'set-credentials'.", flush=True)
        return None


def auto_login(timeout: float = AUTO_LOGIN_SECONDS) -> str | None:
    """Sign in automatically with the stored credentials and return a fresh token.

    Drives the real login page inside the private profile (so any Duo
    'remember this browser' cookie applies): /auth -> UMPASS button -> ADFS form
    -> back to the app. Returns None when a human is needed (two-factor push
    that nobody approved, wrong password, timeout, ...).
    """
    credentials = load_credentials()
    if not credentials:
        return None
    username, password = credentials
    stamp = time.strftime("%d/%b/%Y %H:%M:%S")
    port = existing_debug_port(any_page=True)
    launched: subprocess.Popen[bytes] | None = None
    if port is None:
        # Automatic sign-in needs no window of its own: drive a headless instance
        # against the same private profile (so the Duo device-trust cookie applies)
        # and shut it down again. Leaving a window open here is what piled visible
        # pages up on screen after every renewal (2026-10-30).
        port = free_local_port()
        launched = launch_debug_browser(port, headless=True)
        if launched is not None:
            try:
                private_write(DEBUG_PORT_FILE, f"{port}\n")
            except OSError:
                pass
            wait_until = time.monotonic() + 20.0
            while time.monotonic() < wait_until and not devtools_targets(port):
                if launched.poll() is not None:
                    break
                time.sleep(0.3)
        if not devtools_targets(port):
            if launched is not None:
                stop_process_tree(launched.pid)
                launched = None
            # Fall back to the visible window: only a human can finish some logins.
            open_login_window()
            port = existing_debug_port(any_page=True)
    if port is None:
        return None
    try:
        version = request_json(f"http://127.0.0.1:{port}/json/version")
        browser = DevToolsSession(str(version["webSocketDebuggerUrl"]), timeout=20.0)
    except Exception:
        return None

    target_id = ""
    page = None
    got_token = ""
    try:
        # A new tab in the *default* context: the user's own window keeps its state
        # while we still inherit the profile's cookies (Duo device trust included).
        target_id = str(browser.call("Target.createTarget", {"url": "about:blank"})["targetId"])
        ws_url = ""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not ws_url:
            for item in devtools_targets(port):
                if item.get("id") == target_id:
                    ws_url = str(item.get("webSocketDebuggerUrl") or "")
            if not ws_url:
                time.sleep(0.25)
        if not ws_url:
            return None
        page = DevToolsSession(ws_url, timeout=25.0)
        page.call("Page.enable")

        def evaluate(expression: str) -> str:
            try:
                value = page.call(
                    "Runtime.evaluate", {"expression": expression, "returnByValue": True}
                ).get("result", {}).get("value")
            except (WebSocketError, OSError, ValueError):
                return ""
            return str(value if value is not None else "")

        page.call("Page.navigate", {"url": f"{UPSTREAM}/auth"})
        filled_for = ""
        attempts = 0
        last_action = 0.0
        mfa_notified = False
        bad_password = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.8)
            try:
                snap = json.loads(evaluate(AUTOLOGIN_STATE_JS) or "{}")
            except ValueError:
                continue
            href = str(snap.get("href") or "")
            token = str(snap.get("token") or "")
            if token and probe_um_token(token) is not False:
                got_token = token
                print(f"[{stamp}] auto-login succeeded.", flush=True)
                return token
            if "duosecurity.com" in href:
                if not mfa_notified:
                    mfa_notified = True
                    print(
                        f"[{stamp}] UM wants two-factor approval: approve the Duo prompt on your "
                        "phone (the private window is open) and the proxy recovers by itself.",
                        flush=True,
                    )
                if time.monotonic() - last_action > 5:
                    last_action = time.monotonic()
                    evaluate(AUTOLOGIN_DUO_PUSH_JS)
                continue
            if snap.get("hasPassword"):
                if href != filled_for:
                    filled_for = href
                    evaluate(AUTOLOGIN_FILL_JS % (json.dumps(username), json.dumps(password)))
                    print(
                        f"[{stamp}] filled the UM login form with the stored credentials.",
                        flush=True,
                    )
                if snap.get("error") and not bad_password:
                    bad_password = True
                    attempts += 1
                    print(
                        f"[{stamp}] the login form reported an error after submitting.",
                        flush=True,
                    )
                if time.monotonic() - last_action > 6:
                    last_action = time.monotonic()
                    attempts += 1
                    if attempts > 4:
                        print(
                            f"[{stamp}] auto-login gave up: the login form kept rejecting the "
                            "stored credentials. Run 'set-credentials' with the current password.",
                            flush=True,
                        )
                        return None
                    outcome = evaluate(AUTOLOGIN_SUBMIT_JS)
                    print(f"[{stamp}] submitted the UM login form ({outcome or 'no response'}).", flush=True)
                    if outcome.startswith("{"):
                        control = json.loads(outcome).get("id") or ""
                        evaluate(AUTOLOGIN_CLICK_JS % json.dumps(control))
                    elif outcome.startswith("error"):
                        continue
                continue
            if CHAT_HOST in href and time.monotonic() - last_action > 2.5:
                last_action = time.monotonic()
                evaluate(AUTOLOGIN_UMPASS_JS)
        print(f"[{stamp}] auto-login timed out.", flush=True)
        return None
    except (WebSocketError, OSError, KeyError, ValueError) as exc:
        print(f"[{stamp}] auto-login failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        if page is not None:
            page.close()
        if target_id:
            # Always drop the tab this login created: a failed or timed-out attempt
            # used to leave an /auth page behind, so every retry added another one.
            try:
                browser.call("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass
        if launched is None and got_token and port is not None:
            # We drove a visible login window (reused from an earlier prompt or opened
            # as the fallback). It has done its job now, and leaving it on screen is the
            # leftover the user complained about on 2026-10-30. Browser.close must run
            # before the session is closed below, or it would only raise.
            try:
                browser.call("Browser.close")
            except Exception:
                pass
            try:
                DEBUG_PORT_FILE.unlink()
            except OSError:
                pass
        try:
            browser.close()
        except Exception:
            pass
        if launched is not None:
            # The headless instance existed only for this login: take it down so no
            # stray browser process (or page) survives the cycle.
            stop_process_tree(launched.pid)
            try:
                if DEBUG_PORT_FILE.read_text(encoding="utf-8").strip() == str(port):
                    DEBUG_PORT_FILE.unlink()
            except OSError:
                pass


def monitor_browser_storage(
    timeout: int, home: pathlib.Path | None = None, platform_name: str | None = None
) -> str:
    platform_name = sys.platform if platform_name is None else platform_name
    browser_names = (
        "Safari, Chrome, Edge, Brave, Arc, Vivaldi, Opera, Firefox, or a supported fork"
        if platform_name == "darwin"
        else "Chrome, Chromium, Edge, Brave, Vivaldi, Opera, Firefox, or a supported fork"
    )
    print(
        "Browser monitoring is active. This script will not open or control a browser.\n"
        f"In {browser_names}:\n"
        "  1. Open https://chat.genai.um.edu.mo and log in.\n"
        "  2. Send any chat message. Keep this Terminal window open.\n"
        "If you were already logged in and nothing is detected, sign out and log in once.\n"
        "The setup will validate the UM session and finish automatically."
    )
    deadline = time.monotonic() + timeout
    fingerprints: dict[pathlib.Path, tuple[int, int]] = {}
    rejected_hashes: set[str] = set()
    retry_after: dict[str, float] = {}
    announced_files = False
    while time.monotonic() < deadline:
        files = browser_storage_files(home, platform_name)
        if files and not announced_files:
            browsers = ", ".join(sorted({browser for browser, _path in files}))
            print(f"Watching local storage for: {browsers}")
            announced_files = True
        for browser, path in files:
            try:
                stat = path.stat()
                fingerprint = (stat.st_mtime_ns, stat.st_size)
                if fingerprints.get(path) == fingerprint:
                    continue
                data = path.read_bytes()
                fingerprints[path] = fingerprint
            except OSError:
                continue
            for token in extract_browser_token_candidates(data):
                digest = hashlib.sha256(token.encode()).hexdigest()
                if digest in rejected_hashes or retry_after.get(digest, 0) > time.monotonic():
                    continue
                valid = probe_um_token(token)
                if valid is True:
                    print(f"Authenticated UM session detected in {browser}.")
                    return token
                if valid is False:
                    rejected_hashes.add(digest)
                else:
                    retry_after[digest] = time.monotonic() + 10
        time.sleep(0.75)
    die(
        "Timed out without finding a valid UM browser session. Run setup again, "
        "or use the --curl-file fallback described in STUDENT_GUIDE.md."
    )


def prompt_token(
    curl_file: str | None,
    monitor_timeout: int,
    browser: str | None = None,
    *,
    require_devtools: bool = False,
) -> str:
    if curl_file:
        try:
            copied = pathlib.Path(curl_file).read_text(encoding="utf-8")
        except OSError as exc:
            die(f"Could not read --curl-file: {exc}")
        token = extract_token(copied)
        copied = ""
        if not token:
            die("No Bearer token was found in --curl-file.")
        return token
    want_devtools = require_devtools or IS_WINDOWS or (browser or "auto") != "auto"
    if want_devtools:
        if find_debug_browser(browser):
            return capture_token_via_devtools(monitor_timeout, browser=browser)
        if IS_WINDOWS or require_devtools:
            print("No supported Chromium-family browser was found; watching profile files instead.")
    return monitor_browser_storage(monitor_timeout)


def request_json(url: str, *, token: str | None = None, timeout: float = 30) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def validate_token(token: str) -> list[str]:
    try:
        payload = request_json(f"{UPSTREAM}/api/models", token=token)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            die("The login token was rejected. Reload the chat page, log in again, and retry.")
        die(f"UM GenAI returned HTTP {exc.code} while validating the token.")
    except (urllib.error.URLError, TimeoutError) as exc:
        die(f"Cannot reach UM GenAI: {exc}. Check the network/VPN and retry.")

    if isinstance(payload, list):
        models = payload
    elif isinstance(payload, dict):
        models = payload.get("data") or payload.get("models") or []
    else:
        models = []
    ids = [item.get("id") for item in models if isinstance(item, dict) and item.get("id")]
    if not ids:
        die("Token worked, but UM GenAI returned no models.")
    return ids


def config_payload(port: int) -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": "um-genai/DeepSeek-V4.1-Flash",
        "small_model": "um-genai/DeepSeek-V4.1-Flash",
        "provider": {
            "um-genai": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "UM GenAI (local proxy)",
                "options": {
                    "baseURL": f"http://{DEFAULT_HOST}:{port}/v1",
                    "apiKey": "{file:./local-api-key}",
                    "timeout": False,
                },
                "models": MODEL_CONFIG,
            }
        },
    }


def install_self() -> None:
    ensure_private_dir()
    source = pathlib.Path(__file__).resolve()
    if source != SCRIPT_FILE:
        shutil.copy2(source, SCRIPT_FILE)
    os.chmod(SCRIPT_FILE, 0o700)


def write_launcher() -> None:
    if IS_WINDOWS:
        content = (
            "@echo off\r\n"
            "setlocal\r\n"
            f'"{sys.executable}" "{SCRIPT_FILE}" ensure\r\n'
            "if errorlevel 1 exit /b %errorlevel%\r\n"
            f'set "OPENCODE_CONFIG={CONFIG_FILE}"\r\n'
            "opencode %*\r\n"
        )
        private_write(LAUNCHER_CMD, content)
        return
    content = f"""#!/bin/sh
set -eu
script={shlex.quote(str(SCRIPT_FILE))}
config={shlex.quote(str(CONFIG_FILE))}
"$script" ensure
export OPENCODE_CONFIG="$config"
exec opencode "$@"
"""
    private_write(LAUNCHER, content)
    os.chmod(LAUNCHER, 0o700)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def windows_pid_alive(pid: int) -> bool:
    try:
        raw = (
            subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            ).stdout
            or b""
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return re.search(rb'","' + str(pid).encode("ascii") + rb'"', raw) is not None


def windows_process_command(pid: int) -> str | None:
    script = (
        f'$p = Get-CimInstance Win32_Process -Filter "ProcessId={pid}" '
        "-ErrorAction SilentlyContinue; if ($p) { $p.CommandLine }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=45,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or b"").decode("utf-8", "replace").strip()


def pid_is_running(pid: int) -> bool:
    if pid <= 1:
        return False
    if IS_WINDOWS:
        return windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def current_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if pid_is_running(pid) else None


def health(port: int, timeout: float = 2) -> dict[str, Any] | None:
    try:
        result = request_json(f"http://{DEFAULT_HOST}:{port}/health", timeout=timeout)
        return result if isinstance(result, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def start_proxy(port: int, *, quiet: bool = False) -> None:
    state = health(port)
    if state and state.get("service") == "um-opencode-proxy":
        if not quiet:
            print(f"Proxy is already running on http://{DEFAULT_HOST}:{port}/v1")
        return
    if state:
        die(f"Port {port} is already used by another HTTP service.")
    ensure_private_dir()
    log_handle = open(LOG_FILE, "ab", buffering=0)
    command = [
        sys.executable,
        str(SCRIPT_FILE),
        "serve",
        "--port",
        str(port),
        "--ignore-console-events",
    ]
    launch: dict[str, Any] = {"close_fds": True}
    if IS_WINDOWS:
        launch["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        launch["start_new_session"] = True
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        **launch,
    )
    log_handle.close()
    private_write(PID_FILE, f"{process.pid}\n")
    for _ in range(40):
        if health(port, timeout=0.25):
            if not quiet:
                print(f"Proxy started: http://{DEFAULT_HOST}:{port}/v1")
            return
        if process.poll() is not None:
            break
        time.sleep(0.1)
    die(f"Proxy failed to start. Inspect: {LOG_FILE}")


def stop_proxy(*, quiet: bool = False) -> None:
    pid = current_pid()
    if not pid:
        try:
            PID_FILE.unlink()
        except FileNotFoundError:
            pass
        if not quiet:
            print("Proxy is not running.")
        return
    if IS_WINDOWS:
        command_line = windows_process_command(pid)
        if command_line and (
            "um_opencode_setup.py" not in command_line or " serve" not in command_line
        ):
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                pass
            die(
                f"Refusing to stop PID {pid}: it is not the UM OpenCode proxy. "
                "The stale PID file was removed."
            )
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        check = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            capture_output=True,
        )
        command_line = check.stdout
        if "um_opencode_setup.py" not in command_line or " serve" not in command_line:
            try:
                PID_FILE.unlink()
            except FileNotFoundError:
                pass
            die(
                f"Refusing to stop PID {pid}: it is not the UM OpenCode proxy. "
                "The stale PID file was removed."
            )
        os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not pid_is_running(pid):
            break
        time.sleep(0.1)
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass
    if not quiet:
        print("Proxy stopped.")


def setup(args: argparse.Namespace) -> None:
    print("UM GenAI -> OpenCode classroom setup")
    print("The local proxy binds only to 127.0.0.1 and never displays your UM token.\n")
    token = prompt_token(args.curl_file, args.monitor_timeout, getattr(args, "browser", "auto"))
    model_ids = validate_token(token)
    ensure_private_dir()
    private_write(TOKEN_FILE, token + "\n")
    if not LOCAL_KEY_FILE.exists():
        private_write(LOCAL_KEY_FILE, secrets.token_urlsafe(32) + "\n")
    private_write(CONFIG_FILE, json.dumps(config_payload(args.port), indent=2) + "\n")
    install_self()
    write_launcher()
    start_proxy(args.port)

    print("\nSetup complete.")
    print(f"API Base URL : http://{DEFAULT_HOST}:{args.port}/v1")
    print(f"API Key file : {LOCAL_KEY_FILE}")
    print(f"OpenCode cfg : {CONFIG_FILE}")
    print(f"Models found : {', '.join(model_ids)}")
    print("\nRecommended (no manual API entry):")
    print(f"  {LAUNCHER_CMD if IS_WINDOWS else LAUNCHER} /path/to/your/project")
    print("\nIf a GUI asks for OpenAI-compatible API settings:")
    print(f"  Base URL = http://{DEFAULT_HOST}:{args.port}/v1")
    print(f"  API key  = run: {SCRIPT_FILE} show-key")
    print("  Models   = Qwen3.5-397B-A17B or DeepSeek-V4.1-Flash")
    print("\nThe API key above is only for this computer; it is not the UM session token.")


def refresh(args: argparse.Namespace) -> None:
    token = prompt_token(args.curl_file, args.monitor_timeout, getattr(args, "browser", "auto"))
    models = validate_token(token)
    private_write(TOKEN_FILE, token + "\n")
    print(f"Token refreshed. Models: {', '.join(models)}")
    start_proxy(args.port, quiet=True)


def keepalive(args: argparse.Namespace) -> None:
    """Refresh an expired UM token without any user interaction."""
    token = read_secret(TOKEN_FILE, "UM login token")
    state = probe_um_token(token)
    labels = {True: "valid", False: "rejected by UM", None: "unverifiable (network/server)"}
    print(f"Current UM token : {labels.get(state, 'unknown')}")
    if not args.force:
        if state is True:
            print("Nothing to do: the token still works.")
            return
        if state is None:
            print("Nothing to do while UM is unreachable; retry later.")
            return
    print("Capturing a fresh token silently (no window, no password)...")
    fresh = silent_token_capture(
        timeout=args.monitor_timeout,
        prefer_existing=not args.window,
        headless=not args.window,
    )
    if not fresh:
        die(
            "Silent capture failed. Run 'setup' or 'refresh' to log in again in the "
            "private browser window."
        )
    private_write(TOKEN_FILE, fresh + "\n")
    print("Token refreshed automatically; a running proxy picks it up on the next request.")


def set_credentials(args: argparse.Namespace) -> None:
    """Store the UM username/password so an expired session can be renewed alone."""
    username = args.username or os.environ.get("UM_USERNAME", "")
    password = args.password or os.environ.get("UM_PASSWORD", "")
    if args.stdin:
        line = sys.stdin.readline().rstrip("\n")
        username, _, password = line.partition("\t")
    if not username or not password:
        die(
            "Need a username and password: pass --username/--password, set "
            "UM_USERNAME/UM_PASSWORD, or use --stdin with 'user<TAB>password'."
        )
    save_credentials(username.strip(), password)
    print("Automatic re-login is now enabled (the password never reaches the disk in clear).")


def forget_credentials(_args: argparse.Namespace) -> None:
    try:
        CREDENTIALS_FILE.unlink()
    except FileNotFoundError:
        pass
    print("Stored UM credentials deleted; automatic re-login is off.")


def login(args: argparse.Namespace) -> None:
    """Sign in with the stored credentials and store the resulting token."""
    if load_credentials() is None:
        die(f"No stored credentials. Run: {SCRIPT_FILE} set-credentials")
    print("Signing in to UM with the stored credentials (normally no window appears)...")
    fresh = auto_login(timeout=args.timeout)
    if not fresh:
        die(
            "Automatic sign-in did not complete. If UM asked for two-factor approval, "
            "approve it on your phone and try again; otherwise run 'refresh'."
        )
    private_write(TOKEN_FILE, fresh + "\n")
    models = validate_token(fresh)
    print(f"Signed in. Models: {', '.join(models)}")
    print("A running proxy picks the new token up on the next request.")


def ensure(args: argparse.Namespace) -> None:
    if not TOKEN_FILE.exists() or not CONFIG_FILE.exists() or not LOCAL_KEY_FILE.exists():
        die(f"Setup is incomplete. Run: {SCRIPT_FILE} setup")
    start_proxy(args.port, quiet=True)


def upstream_diagnosis(token: str) -> str:
    """Explain the UM gateway state.

    Keeps a UM-side outage from looking like a fault of the local proxy: the
    gateway itself answers 503 with an Envoy "upstream connect error" body when
    its own model backend is unreachable.
    """
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "UMOpenCodeProxy/1.2",
    }
    try:
        connection = http.client.HTTPSConnection(
            "chat.genai.um.edu.mo", 443, timeout=20, context=ssl.create_default_context()
        )
        connection.request("GET", "/api/models", headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8", "replace")
        connection.close()
    except (OSError, http.client.HTTPException) as exc:
        return f"unreachable ({exc.__class__.__name__}) - check this machine's network"
    if response.status == 200:
        return "healthy - gateway accepted the token"
    if response.status in (401, 403):
        return f"HTTP {response.status} - UM rejected the login token; run 'refresh'"
    if response.status == 503 and "upstream connect error" in body:
        detail = re.search(r"transport failure reason: [^,]*|reset reason: [^,]*", body)
        reason = detail.group(0).strip() if detail else "backend unreachable"
        return f"HTTP 503 - UM's own backend is down ({reason}); local setup is fine, retry later"
    return f"HTTP {response.status} - {body.strip()[:140] or 'no body'}"


def status(args: argparse.Namespace) -> None:
    token = read_secret(TOKEN_FILE, "UM login token")
    token_payload = decode_jwt_payload(token)
    state = health(args.port)
    print(f"Install directory : {INSTALL_DIR}")
    print(f"Proxy             : {'running' if state else 'stopped'}")
    print(f"Upstream (UM)     : {upstream_diagnosis(token)}")
    print(f"API base URL       : http://{DEFAULT_HOST}:{args.port}/v1")
    print(f"OpenCode installed : {'yes' if command_exists('opencode') else 'no'}")
    if token_payload and isinstance(token_payload.get("exp"), (int, float)):
        expires = dt.datetime.fromtimestamp(token_payload["exp"], dt.timezone.utc)
        print(f"Token expires      : {expires.isoformat()}")
    else:
        print("Token expires      : not stated by the server; refresh after 401/403")


def show_key() -> None:
    print(read_secret(LOCAL_KEY_FILE, "local API key"))


def test(args: argparse.Namespace) -> None:
    ensure(args)
    key = read_secret(LOCAL_KEY_FILE, "local API key")
    request_body = json.dumps(
        {
            "model": "Qwen3.5-397B-A17B",
            "stream": False,
            "messages": [{"role": "user", "content": "Reply with exactly: UM_API_OK"}],
        }
    ).encode()
    request = urllib.request.Request(
        f"http://{DEFAULT_HOST}:{args.port}/v1/chat/completions",
        data=request_body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        die(f"API test failed with HTTP {exc.code}: {detail[:500]}")
    content = payload.get("choices", [{}])[0].get("message", {}).get("content")
    if content != "UM_API_OK":
        die(f"API replied unexpectedly: {content!r}")
    print("Local API test: PASS")

    deepseek_body = json.dumps(
        {
            "model": DEEPSEEK_NATIVE_MODEL or "DeepSeek-V4.1-Flash",
            "stream": False,
            "temperature": 0,
            "max_tokens": 256,
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "native_probe",
                        "description": "Return the requested probe value.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    },
                }
            ],
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Call native_probe with value DEEPSEEK_NATIVE_OK. "
                        "Do not answer without calling the tool."
                    ),
                }
            ],
        }
    ).encode()
    deepseek_request = urllib.request.Request(
        f"http://{DEFAULT_HOST}:{args.port}/v1/chat/completions",
        data=deepseek_body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(deepseek_request, timeout=60) as response:
            deepseek_payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        die(f"DeepSeek native-tool test failed with HTTP {exc.code}: {detail[:500]}")
    choice = deepseek_payload.get("choices", [{}])[0]
    calls = choice.get("message", {}).get("tool_calls") or []
    if choice.get("finish_reason") != "tool_calls" or not calls:
        die(f"DeepSeek did not return a native tool call: {choice!r}")
    function = calls[0].get("function", {})
    try:
        arguments = json.loads(function.get("arguments", "{}"))
    except json.JSONDecodeError:
        arguments = {}
    if function.get("name") != "native_probe" or arguments.get("value") != "DEEPSEEK_NATIVE_OK":
        die(f"DeepSeek returned an unexpected tool call: {function!r}")
    print("DeepSeek native tools: PASS")

    if command_exists("opencode") and not args.skip_opencode:
        env = os.environ.copy()
        env["OPENCODE_CONFIG"] = str(CONFIG_FILE)
        command = [
            "opencode",
            "run",
            "--pure",
            "-m",
            "um-genai/Qwen3.5-397B-A17B",
            "Reply with exactly: OPENCODE_OK",
        ]
        result = subprocess.run(
            command, env=env, text=True, capture_output=True, timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0 or "OPENCODE_OK" not in result.stdout:
            die(f"OpenCode test failed:\n{(result.stderr or result.stdout)[-1000:]}")
        print("OpenCode test : PASS")
    elif not command_exists("opencode"):
        print("OpenCode test : SKIPPED (opencode command not installed)")


def uninstall() -> None:
    stop_proxy(quiet=True)
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
    print(f"Removed {INSTALL_DIR}, including the saved UM token and local API key.")


def normalize_models(body: bytes) -> bytes:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if isinstance(payload, list):
        payload = {"object": "list", "data": payload}
    elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
        payload = {"object": "list", "data": payload["models"]}
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        payload.setdefault("object", "list")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def message_content_text(content: Any) -> str:
    """Flatten the text portions of an OpenAI message or tool result."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
                elif block.get("type") in {"image", "image_url"}:
                    parts.append("[image omitted from tool result]")
        return "\n\n".join(parts)
    return str(content)


def render_deepseek_tools(tools: list[Any]) -> str:
    schemas = []
    for tool in tools:
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict):
            schemas.append(
                json.dumps(tool["function"], ensure_ascii=False, separators=(",", ":"))
            )
    return DSML_TOOLS_TEMPLATE.format(dsml=DSML_TOKEN, tool_schemas="\n".join(schemas))


def openai_tool_calls_to_dsml(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list):
        return ""
    invokes: list[str] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict) or not isinstance(tool_call.get("function"), dict):
            continue
        function = tool_call["function"]
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw_arguments = function.get("arguments", "{}")
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except (TypeError, json.JSONDecodeError):
            arguments = {"arguments": str(raw_arguments)}
        if not isinstance(arguments, dict):
            arguments = {"arguments": arguments}
        parameters = []
        for key, value in arguments.items():
            is_string = isinstance(value, str)
            encoded = value if is_string else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            parameters.append(
                f'<{DSML_TOKEN}parameter name="{key}" string="{str(is_string).lower()}">'
                f"{encoded}</{DSML_TOKEN}parameter>"
            )
        body = "\n".join(parameters)
        invokes.append(
            f'<{DSML_TOKEN}invoke name="{name}">\n{body}\n</{DSML_TOKEN}invoke>'
        )
    if not invokes:
        return ""
    return (
        f"<{DSML_TOKEN}tool_calls>\n"
        + "\n".join(invokes)
        + f"\n</{DSML_TOKEN}tool_calls>"
    )


def prepare_deepseek_native_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Bridge OpenAI tools to the DSML format native to DeepSeek V4."""
    tools = payload.get("tools")
    has_tool_history = any(
        isinstance(message, dict)
        and (message.get("role") == "tool" or message.get("tool_calls"))
        for message in payload.get("messages", [])
    )
    if payload.get("model") != DEEPSEEK_NATIVE_MODEL or not (tools or has_tool_history):
        return payload, False

    adapted = dict(payload)
    adapted_tools = adapted.pop("tools", [])
    adapted.pop("tool_choice", None)
    adapted.pop("parallel_tool_calls", None)
    adapted.pop("stream_options", None)
    adapted["stream"] = False

    messages: list[dict[str, Any]] = []
    for original in payload.get("messages", []):
        if not isinstance(original, dict):
            continue
        message = dict(original)
        if message.get("role") == "assistant" and message.get("tool_calls"):
            dsml = openai_tool_calls_to_dsml(message.pop("tool_calls"))
            existing = message_content_text(message.get("content"))
            message["content"] = "\n\n".join(part for part in (existing, dsml) if part)
        if message.get("role") == "tool":
            result = f"<tool_result>{message_content_text(message.get('content'))}</tool_result>"
            if messages and messages[-1].get("role") == "user" and messages[-1].get(
                "_um_tool_result"
            ):
                messages[-1]["content"] += "\n\n" + result
                continue
            message = {"role": "user", "content": result, "_um_tool_result": True}
        messages.append(message)

    for message in messages:
        message.pop("_um_tool_result", None)

    if adapted_tools:
        tools_prompt = render_deepseek_tools(adapted_tools)
        system = next((message for message in messages if message.get("role") == "system"), None)
        if system is None:
            messages.insert(0, {"role": "system", "content": tools_prompt})
        else:
            existing = message_content_text(system.get("content"))
            system["content"] = "\n\n".join(part for part in (existing, tools_prompt) if part)
    adapted["messages"] = messages
    return adapted, True


DSML_TOOL_BLOCK_RE = re.compile(
    rf"<{re.escape(DSML_TOKEN)}tool_calls>\s*(.*?)\s*</{re.escape(DSML_TOKEN)}tool_calls>",
    flags=re.DOTALL,
)
DSML_INVOKE_RE = re.compile(
    rf'<{re.escape(DSML_TOKEN)}invoke name="([^"]+)">\s*(.*?)\s*</{re.escape(DSML_TOKEN)}invoke>',
    flags=re.DOTALL,
)
DSML_PARAMETER_RE = re.compile(
    rf'<{re.escape(DSML_TOKEN)}parameter name="([^"]+)" string="(true|false)">(.*?)</{re.escape(DSML_TOKEN)}parameter>',
    flags=re.DOTALL,
)


def parse_deepseek_dsml(content: Any) -> tuple[str | None, list[dict[str, Any]]]:
    if not isinstance(content, str):
        return content, []
    tool_calls: list[dict[str, Any]] = []
    for block in DSML_TOOL_BLOCK_RE.findall(content):
        for name, invocation in DSML_INVOKE_RE.findall(block):
            arguments: dict[str, Any] = {}
            for key, is_string, raw_value in DSML_PARAMETER_RE.findall(invocation):
                if is_string == "true":
                    arguments[key] = raw_value
                else:
                    try:
                        arguments[key] = json.loads(raw_value)
                    except json.JSONDecodeError:
                        arguments[key] = raw_value
            tool_calls.append(
                {
                    "id": "call_" + secrets.token_hex(12),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(
                            arguments, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                }
            )
    remaining = DSML_TOOL_BLOCK_RE.sub("", content).strip()
    return remaining or None, tool_calls


def adapt_deepseek_native_response(payload: dict[str, Any]) -> dict[str, Any]:
    for choice in payload.get("choices", []):
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            continue
        message = choice["message"]
        content, tool_calls = parse_deepseek_dsml(message.get("content"))
        message["content"] = content
        if tool_calls:
            message["tool_calls"] = tool_calls
            choice["finish_reason"] = "tool_calls"
    return payload


def openai_json_to_sse(payload: dict[str, Any]) -> bytes:
    """Create a valid OpenAI chat-completions SSE stream from one JSON response."""
    common = {
        "id": payload.get("id", "chatcmpl-um-proxy"),
        "object": "chat.completion.chunk",
        "created": payload.get("created", int(time.time())),
        "model": payload.get("model", DEEPSEEK_NATIVE_MODEL or "DeepSeek-V4.1-Flash"),
    }
    events: list[dict[str, Any]] = []
    for choice in payload.get("choices", []):
        if not isinstance(choice, dict):
            continue
        index = choice.get("index", 0)
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        delta: dict[str, Any] = {"role": message.get("role", "assistant")}
        if message.get("content") is not None:
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": tool_index, **tool_call}
                for tool_index, tool_call in enumerate(message["tool_calls"])
            ]
        events.append({**common, "choices": [{"index": index, "delta": delta, "finish_reason": None}]})
        events.append(
            {
                **common,
                "choices": [
                    {
                        "index": index,
                        "delta": {},
                        "finish_reason": choice.get("finish_reason", "stop"),
                    }
                ],
            }
        )
    encoded = [
        "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        for event in events
    ]
    encoded.append("data: [DONE]\n\n")
    return "".join(encoded).encode()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UMOpenCodeProxy/1.2"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.client_address[0]} {fmt % args}\n")

    def json_response(self, status_code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def local_authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        expected = read_secret(LOCAL_KEY_FILE, "local API key")
        return secrets.compare_digest(supplied, expected)

    def upstream(self, method: str, path: str, body: bytes | None = None):
        token = read_secret(TOKEN_FILE, "UM login token")
        connection = http.client.HTTPSConnection(
            "chat.genai.um.edu.mo", 443, timeout=600, context=ssl.create_default_context()
        )
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": self.server_version,
        }
        connection.request(method, path, body=body, headers=headers)
        return connection, connection.getresponse()

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path in {"/", "/health"}:
            self.json_response(200, {"status": "ok", "service": "um-opencode-proxy"})
            return
        if path != "/v1/models":
            self.json_response(404, {"error": {"message": f"Unsupported route: {path}"}})
            return
        if not self.local_authorized():
            self.json_response(401, {"error": {"message": "Invalid local API key"}})
            return
        connection = None
        try:
            connection, response = self.upstream("GET", "/api/models")
            body = response.read()
            if response.status in (401, 403) and renew_token_once():
                connection.close()
                connection, response = self.upstream("GET", "/api/models")
                body = response.read()
            if response.status == 200:
                body = normalize_models(body)
            elif response.status in (401, 403):
                open_login_window()
                body = json.dumps(
                    {"error": {"message": SESSION_EXPIRED_MESSAGE, "type": "um_session_expired"}}
                ).encode()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (OSError, http.client.HTTPException) as exc:
            self.json_response(502, {"error": {"message": f"Upstream request failed: {exc}"}})
        finally:
            if connection:
                connection.close()

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if path != "/v1/chat/completions":
            self.json_response(404, {"error": {"message": f"Unsupported route: {path}"}})
            return
        if not self.local_authorized():
            self.json_response(401, {"error": {"message": "Invalid local API key"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.json_response(400, {"error": {"message": "Invalid Content-Length"}})
            return
        if length <= 0 or length > 32 * 1024 * 1024:
            self.json_response(413, {"error": {"message": "Body is empty or too large"}})
            return
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.json_response(400, {"error": {"message": "Body must be JSON"}})
            return
        if not isinstance(payload, dict) or not payload.get("model") or not isinstance(
            payload.get("messages"), list
        ):
            self.json_response(400, {"error": {"message": "model and messages are required"}})
            return
        client_stream = payload.get("stream") is True
        upstream_payload, deepseek_native = prepare_deepseek_native_payload(payload)
        clean_body = json.dumps(
            upstream_payload, ensure_ascii=False, separators=(",", ":")
        ).encode()
        connection = None
        try:
            connection, response = self.upstream("POST", "/api/chat/completions", clean_body)
            if response.status in (401, 403):
                # The UM session expired while the proxy was running: renew once and
                # retry, so an expiry the watchdog has not seen yet stays invisible.
                try:
                    response.read()
                except OSError:
                    pass
                connection.close()
                if renew_token_once():
                    connection, response = self.upstream(
                        "POST", "/api/chat/completions", clean_body
                    )
                else:
                    open_login_window()
                    self.json_response(
                        401,
                        {
                            "error": {
                                "message": SESSION_EXPIRED_MESSAGE,
                                "type": "um_session_expired",
                            }
                        },
                    )
                    return
            content_type = response.getheader("Content-Type", "application/octet-stream")
            if "application/json" in content_type.lower():
                response_body = response.read()
                if deepseek_native and response.status == 200:
                    try:
                        response_payload = json.loads(response_body)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self.json_response(
                            502,
                            {"error": {"message": "DeepSeek returned malformed JSON"}},
                        )
                        return
                    response_payload = adapt_deepseek_native_response(response_payload)
                    if client_stream:
                        stream_body = openai_json_to_sse(response_payload)
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Content-Length", str(len(stream_body)))
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(stream_body)
                        return
                    response_body = json.dumps(
                        response_payload, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                self.send_response(response.status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(response_body)
                return
            self.send_response(response.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.log_message("client disconnected during stream")
        except (OSError, http.client.HTTPException) as exc:
            self.json_response(502, {"error": {"message": f"Upstream request failed: {exc}"}})
        finally:
            if connection:
                connection.close()


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # listening, which silently runs two proxies (two watchdogs, two login
    # windows fighting over one profile). Refuse the second bind instead.
    allow_reuse_address = not IS_WINDOWS


def ignore_console_events() -> None:
    """Make the daemon immune to console Ctrl+C/Ctrl+Break/Ctrl+Close events.

    On Windows a console control event is delivered to every process attached to
    the console, so an unrelated Ctrl+C in a shared console (for example the
    harness host) can kill this background proxy. Ignoring the events keeps the
    daemon alive; stop it with the `stop` subcommand or taskkill instead.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
    except (ImportError, AttributeError, OSError):
        pass
    for name in ("SIGINT", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, signal.SIG_IGN)
        except (OSError, ValueError):
            pass


def redirect_output_to_log(path: pathlib.Path) -> None:
    """Attach stdout and stderr to *path*.

    Needed when the daemon runs without a console (pythonw.exe started by Task
    Scheduler): there sys.stdout and sys.stderr are None, so the request log and
    any traceback would otherwise be discarded.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    except OSError:
        return
    try:
        os.dup2(handle, 1)
        os.dup2(handle, 2)
    finally:
        if handle > 2:
            os.close(handle)
    sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8", errors="replace")
    sys.stderr = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="replace")


def serve(args: argparse.Namespace) -> None:
    os.umask(0o077)
    if getattr(args, "ignore_console_events", False):
        ignore_console_events()
    log_file = getattr(args, "log_file", None)
    if log_file:
        redirect_output_to_log(pathlib.Path(log_file))
    try:
        ensure_private_dir()
        private_write(PID_FILE, f"{os.getpid()}\n")
    except OSError as exc:
        print(
            f"[{time.strftime('%d/%b/%Y %H:%M:%S')}] could not write {PID_FILE} ({exc})",
            flush=True,
        )
    if log_file:
        print(
            f"[{time.strftime('%d/%b/%Y %H:%M:%S')}] start UM proxy pid={os.getpid()} port={args.port}",
            flush=True,
        )
        atexit.register(
            lambda: print(
                f"[{time.strftime('%d/%b/%Y %H:%M:%S')}] proxy exiting pid={os.getpid()}",
                flush=True,
            )
        )
    try:
        server = ProxyServer((DEFAULT_HOST, args.port), ProxyHandler)
    except OSError as exc:
        die(
            f"Port {args.port} is already in use ({exc}). Another proxy instance is "
            "probably running; this one exits instead of running twice."
        )
    if not getattr(args, "no_auto_refresh", False):
        threading.Thread(
            target=token_watchdog, args=(AUTO_REFRESH_SECONDS,), daemon=True
        ).start()
    print(f"UM OpenCode proxy listening on http://{DEFAULT_HOST}:{args.port}/v1", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if current_pid() == os.getpid():
            try:
                PID_FILE.unlink()
            except OSError:
                pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Set up UM GenAI as a local OpenAI-compatible API for OpenCode."
    )
    sub = result.add_subparsers(dest="command")
    for name, help_text in (
        ("setup", "first-time setup or replace all generated settings"),
        ("refresh", "replace an expired UM login token"),
        ("keepalive", "refresh an expired token silently, without any window"),
        ("start", "start the local proxy"),
        ("ensure", "quietly start the proxy if needed"),
        ("status", "show non-sensitive status"),
        ("test", "test the API and OpenCode"),
        ("serve", "run the foreground proxy (internal)"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument("--port", type=int, default=DEFAULT_PORT)
        if name == "serve":
            child.add_argument(
                "--ignore-console-events",
                action="store_true",
                help="keep running when a console Ctrl+C is delivered to this console",
            )
            child.add_argument(
                "--log-file",
                help="append stdout/stderr to this file (needed without a console)",
            )
            child.add_argument(
                "--no-auto-refresh",
                action="store_true",
                help="do not start the background thread that renews an expired token",
            )
        if name == "keepalive":
            child.add_argument(
                "--force",
                action="store_true",
                help="capture a token even when the current one still validates",
            )
            child.add_argument(
                "--window",
                action="store_true",
                help="use a visible Chrome window instead of the default headless capture",
            )
            child.add_argument(
                "--monitor-timeout",
                type=int,
                default=90,
                help="seconds to wait for the silent token capture",
            )
        if name in {"setup", "refresh"}:
            child.add_argument("--curl-file", help="read Copy-as-cURL text from a local file")
            child.add_argument(
                "--browser",
                default="auto",
                choices=("auto", "chrome", "edge", "brave", "vivaldi", "chromium"),
                help="Chromium-family browser for the assisted login window",
            )
            child.add_argument(
                "--monitor-timeout",
                type=int,
                default=600,
                help="seconds to monitor browser storage for a UM login",
            )
        if name == "test":
            child.add_argument("--skip-opencode", action="store_true")
    sub.add_parser("stop", help="stop the local proxy")
    sub.add_parser("show-key", help="print the local-only API key for GUI setup")
    sub.add_parser("uninstall", help="stop proxy and remove saved credentials/config")
    credentials = sub.add_parser(
        "set-credentials",
        help="store the UM username/password (DPAPI-encrypted) for automatic re-login",
    )
    credentials.add_argument("--username", help="defaults to $UM_USERNAME")
    credentials.add_argument("--password", help="defaults to $UM_PASSWORD (keeps it out of argv)")
    credentials.add_argument(
        "--stdin",
        action="store_true",
        help="read 'username<TAB>password' from standard input",
    )
    sub.add_parser("forget-credentials", help="delete the stored UM username/password")
    login = sub.add_parser("login", help="sign in automatically with the stored credentials")
    login.add_argument("--timeout", type=float, default=AUTO_LOGIN_SECONDS)
    login.add_argument("--window", action="store_true", help="accepted for symmetry (always visible)")
    return result


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parser().parse_args()
    command = args.command or "setup"
    actions = {
        "setup": setup,
        "refresh": refresh,
        "keepalive": keepalive,
        "start": lambda value: start_proxy(value.port),
        "ensure": ensure,
        "status": status,
        "test": test,
        "serve": serve,
        "stop": lambda _value: stop_proxy(),
        "show-key": lambda _value: show_key(),
        "uninstall": lambda _value: uninstall(),
        "set-credentials": set_credentials,
        "forget-credentials": forget_credentials,
        "login": login,
    }
    actions[command](args)


if __name__ == "__main__":
    main()
