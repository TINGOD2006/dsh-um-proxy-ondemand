#!/usr/bin/env python
"""Keep the UM GenAI model lists in sync with what the gateway actually serves.

The local proxy exposes an OpenAI-compatible /v1/models endpoint, but DSH's
profile (``~/.dsh/profiles/desktop/cordis.patch.yml``) and OpenCode
(``~/.um-opencode/opencode.json``) both hard-code the model ids. When the
gateway renames a model (``DeepSeek-V4.1-Flash`` -> ``DeepSeek-V4.1-Flash-1017``),
those configs go stale and the stale id answers HTTP 400 "model not found".

This script compares the live list with both configs, and with ``--apply``
rewrites only the model entries while keeping every other setting untouched:

* the ``um-genai`` ``models:`` list in cordis.patch.yml,
* the ``provider.um-genai.models`` object in opencode.json,
* ``model:`` / ``small_model:`` in opencode.json,
* any ``- provider: um-genai`` row's ``model:`` (subagent allowed models).

Metadata is taken from the **gateway itself** first, then from ``um-models.json``
next to this script, then from built-in defaults:

* ``name`` -- the gateway's ``name`` field when it is a human label, otherwise the
  registry's curated display name, otherwise a name derived from the id;
* ``contextWindow`` -- the gateway's ``max_model_len`` (the real served window),
  falling back to the registry value;
* ``attachment`` (image/file input) -- the gateway's ``info.meta.capabilities``
  (``vision`` / ``file_upload`` / ``file_context``), falling back to the registry;
* ``maxTokens`` (output cap) and ``tool_call`` / ``temperature`` -- registry only,
  because the gateway does not publish them.

The registry is keyed by *family*: the upstream id with its trailing version
suffix removed (``DeepSeek-V4.1-Flash-1017`` -> ``DeepSeek-V4.1-Flash``), so a
renamed model keeps its curated metadata instead of being dropped.

Every run ends with a verification pass (``核對``): the freshly planned text is fed
back through the rebuilder (it must come out unchanged), every id must exist on
the gateway, names must be unique, and ``maxTokens`` may not exceed
``contextWindow``. A failed check exits ``5`` and leaves the backups in place.

Usage:
    python scripts/um-models-sync.py            # report drift + verification, change nothing
    python scripts/um-models-sync.py --apply    # rewrite the configs (with backups)
    python scripts/um-models-sync.py --auto     # apply only when there is drift (Task Scheduler)
    python scripts/um-models-sync.py --target dsh|opencode|both

Exit codes: 0 in sync, 10 drift found (check mode), 20 applied, 2 proxy down,
3 cannot authenticate to the proxy, 4 configuration problem, 5 verification failed.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import uuid
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import contextmanager, ExitStack

HOME = pathlib.Path.home()
DEFAULT_PROFILE = HOME / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
DEFAULT_OPENCODE = HOME / ".um-opencode" / "opencode.json"
DEFAULT_KEY = HOME / ".um-opencode" / "local-api-key"
DEFAULT_LOG = HOME / ".um-opencode" / "um-models-sync.log"
DEFAULT_TOKEN = HOME / ".um-opencode" / "upstream-token"
UPSTREAM_BASE = "https://chat.genai.um.edu.mo"

# Set to "gateway" when the catalog came straight from UM because the local
# proxy was asleep; main() logs that, so a night-time run stays explainable.
CATALOG_SOURCE = "proxy"
REGISTRY = pathlib.Path(__file__).with_name("um-models.json")

VERSION_SUFFIX = re.compile(r"(?<=[A-Za-z])[-_.]v?(\d{2,4})$")


class SyncError(RuntimeError):
    pass


def log(message: str, log_file: pathlib.Path | None) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    # A scheduled run under pythonw has no stdout at all: printing would raise.
    try:
        print(line)
    except (AttributeError, OSError, ValueError):
        pass
    if log_file is not None:
        try:
            with open(log_file, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass


# ---------------------------------------------------------------- upstream


def read_api_key(path: pathlib.Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SyncError(f"cannot read the proxy api key {path}: {exc}") from exc
    if not key:
        raise SyncError(f"the proxy api key {path} is empty")
    return key


def read_upstream_token(path: pathlib.Path = DEFAULT_TOKEN) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def validate_catalog(payload, source: str) -> list[dict]:
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise SyncError(f"{source} returned an empty or invalid model array")
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise SyncError(f"{source} returned an invalid model entry")
        if item["id"] in seen:
            raise SyncError(f"{source} returned duplicate model ids")
        seen.add(item["id"])
    return items


def fetch_catalog_direct(timeout: float = 15.0) -> list[dict]:
    """Ask the UM gateway itself, so a sleeping proxy does not skip the sync.

    The proxy is on-demand: at night it is stopped and ``--auto`` used to log
    "skipped: the proxy is not reachable", which silently disabled model sync
    for those hours. The gateway publishes the same model objects (id, name,
    max_model_len, capabilities), so the comparison can run without it.
    """
    global CATALOG_SOURCE
    token = read_upstream_token()
    if not token:
        raise SyncError("the proxy is asleep and no upstream token is stored")
    request = urllib.request.Request(
        UPSTREAM_BASE + "/api/models",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "UMOpenCodeProxy/1.2",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("model catalog exceeds 2 MiB")
            payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise SyncError("model endpoint returned invalid JSON") from exc
    except urllib.error.HTTPError as exc:
        exc.close()
        raise SyncError(
            f"the UM gateway answered HTTP {exc.code} (run scripts/um-renew.py --force if the token was rejected)"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SyncError(f"neither the proxy nor the UM gateway is reachable: {exc}") from exc
    catalog = validate_catalog(payload, "UM gateway")
    CATALOG_SOURCE = "gateway"
    return catalog


def fetch_catalog(base_url: str, api_key: str, timeout: float = 15.0) -> list[dict]:
    """The full model objects the gateway publishes (id, name, max_model_len, capabilities)."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("model catalog exceeds 2 MiB")
            payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise SyncError("model endpoint returned invalid JSON") from exc
    except urllib.error.HTTPError as exc:
        exc.close()
        raise SyncError(f"the proxy answered HTTP {exc.code} for /v1/models (is the api key right?)") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        try:
            return fetch_catalog_direct(timeout)
        except SyncError as fallback:
            raise SyncError(
                f"the proxy is not reachable at {base_url}: {exc}; {fallback}"
            ) from exc
    return validate_catalog(payload, "proxy")


def fetch_models(base_url: str, api_key: str, timeout: float = 15.0) -> list[str]:
    return [item["id"] for item in fetch_catalog(base_url, api_key, timeout)]


def family(model_id: str) -> str:
    return VERSION_SUFFIX.sub("", model_id)


def resolve(model_id: str, upstream: list[str]) -> str | None:
    """Map an id written in a config onto the id the gateway serves now."""
    if model_id in upstream:
        return model_id
    candidates = [item for item in upstream if family(item) == family(model_id)]
    if not candidates:
        return None
    versioned = [item for item in candidates if item != family(item)]
    return (versioned or candidates)[-1]


# ---------------------------------------------------------------- registry


def load_registry(path: pathlib.Path) -> dict:
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SyncError(f"cannot read the model registry {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SyncError(f"the model registry {path} is not valid JSON: {exc}") from exc
    if not isinstance(registry, dict):
        raise SyncError("the model registry must be a JSON object")
    for field in ("provider", "families"):
        if not isinstance(registry.get(field, {}), dict):
            raise SyncError(f"registry {field} must be an object")
    if any(not isinstance(value, dict) for value in registry.get("families", {}).values()):
        raise SyncError("registry family entries must be objects")
    return registry


def pretty_name(model_id: str) -> str:
    """Human label derived from the id when neither the gateway nor the registry has one."""
    words = [word for word in re.split(r"[-_]+", family(model_id)) if word]
    pretty = " ".join(word if re.fullmatch(r"[A-Z0-9.]+", word) else word[:1].upper() + word[1:] for word in words)
    return f"{pretty} (UM)"


def upstream_name_of(info: dict) -> str:
    name = info.get("name")
    return name.strip() if isinstance(name, str) else ""


def upstream_context_of(info: dict) -> int | None:
    value = info.get("max_model_len")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def upstream_attachment_of(info: dict) -> bool | None:
    info_block = info.get("info") if isinstance(info.get("info"), dict) else {}
    meta = info_block.get("meta") if isinstance(info_block.get("meta"), dict) else {}
    capabilities = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
    keys = ("vision", "file_upload", "file_context")
    if not any(key in capabilities for key in keys):
        return None
    return any(capabilities.get(key) is True for key in keys)


def modalities_for(attachment: bool) -> list[str]:
    """DSH's ``input`` modalities: an image-capable model must declare ``image`` explicitly.

    The pi-ai adapter defaults every hand-written model to ``['text']`` and refuses an
    attachment before sending it, so the gateway's own capabilities decide the list.
    """
    return ["text", "image"] if attachment else ["text"]


def metadata_for(model_id: str, registry: dict, info: dict, warnings: list[str]) -> dict:
    """Gateway metadata wins, then the curated registry, then built-in defaults."""
    provider = registry.get("provider", {})
    families = registry.get("families", {})
    entry = families.get(family(model_id)) or families.get(model_id)
    if entry is None:
        for key, value in families.items():
            if key.lower() == family(model_id).lower():
                entry = value
                break

    gateway_name = upstream_name_of(info)
    gateway_context = upstream_context_of(info)
    gateway_attachment = upstream_attachment_of(info)
    known_names = {model_id.lower(), family(model_id).lower()}
    default_context = provider.get("defaultContextWindow", 131072)
    default_max = provider.get("defaultMaxTokens", 32768)

    if entry is None:
        warnings.append(
            f"no registry entry for model family {family(model_id)!r}; deriving name and limits from the gateway"
        )
        context = gateway_context or default_context
        name_source = "gateway" if gateway_name and gateway_name.lower() not in known_names else "derived"
        return {
            "display": gateway_name if name_source == "gateway" else pretty_name(model_id),
            "contextWindow": context,
            "maxTokens": min(default_max, context),
            "tool_call": True,
            "temperature": True,
            "attachment": bool(gateway_attachment),
            "input": modalities_for(bool(gateway_attachment)),
            "nameSource": name_source,
            "ctxSource": "gateway" if gateway_context else "default",
        }

    if gateway_name and gateway_name.lower() not in known_names:
        display, name_source = gateway_name, "gateway"
    else:
        display, name_source = entry.get("display", model_id), "registry"
    context = gateway_context or int(entry.get("contextWindow", default_context))
    attachment = gateway_attachment if gateway_attachment is not None else bool(entry.get("attachment", False))
    return {
        "display": display,
        "contextWindow": context,
        "maxTokens": min(int(entry.get("maxTokens", default_max)), context),
        "tool_call": entry.get("tool_call", True),
        "temperature": entry.get("temperature", True),
        "attachment": attachment,
        "input": modalities_for(attachment),
        "nameSource": name_source,
        "ctxSource": "gateway" if gateway_context else "registry",
    }


def entries_for(catalog: list[dict], registry: dict, warnings: list[str]) -> list[dict]:
    """Curated order first (the registry), unknown upstream models appended after it."""
    by_id = {item["id"]: item for item in catalog}
    upstream = list(by_id)
    ordered: list[str] = []
    claimed: set[str] = set()
    for name in registry.get("families", {}):
        matches = [item for item in upstream if family(item) == name and item not in claimed]
        if matches:
            ordered.append(matches[0])
            claimed.add(matches[0])
    for model_id in upstream:
        if model_id not in claimed:
            ordered.append(model_id)
            claimed.add(model_id)
    entries = []
    for model_id in ordered:
        metadata = metadata_for(model_id, registry, by_id[model_id], warnings)
        entries.append({"id": model_id, **metadata})
    return entries


def report(entries: list[dict], upstream: list[str]) -> str:
    """The 核對 summary: what will be written and where each value came from."""
    versioned = [model_id for model_id in upstream if model_id != family(model_id)]
    names = Counter(entry["nameSource"] for entry in entries)
    contexts = Counter(entry["ctxSource"] for entry in entries)
    header = (
        f"gateway: {len(upstream)} models, {len(versioned)} versioned id(s)"
        f" ({', '.join(versioned) or 'none'}); names from {dict(names)}; contextWindow from {dict(contexts)}"
    )
    rows = [
        f"  {entry['id']} | {entry['display']} | ctx {entry['contextWindow']} | out {entry['maxTokens']}"
        for entry in entries
    ]
    return "\n".join([header, *rows])


def verify(
    entries: list[dict],
    upstream: list[str],
    plans: list[tuple[pathlib.Path, str, list[str], str]],
) -> list[str]:
    """The final cross-check between the gateway, the planned entries and the planned files."""
    failures: list[str] = []
    ids = [entry["id"] for entry in entries]
    duplicates = sorted({model_id for model_id in ids if ids.count(model_id) > 1})
    if duplicates:
        failures.append(f"duplicate ids in the planned list: {', '.join(duplicates)}")
    missing = [model_id for model_id in ids if model_id not in upstream]
    if missing:
        failures.append(f"planned ids the gateway does not serve: {', '.join(missing)}")
    names = [entry["display"] for entry in entries]
    if any(not name.strip() for name in names):
        failures.append("a planned entry has an empty display name")
    duplicated_names = sorted({name for name in names if names.count(name) > 1})
    if duplicated_names:
        failures.append(f"duplicate display names: {', '.join(duplicated_names)}")
    for entry in entries:
        if entry["contextWindow"] <= 0:
            failures.append(f"{entry['id']}: contextWindow must be positive")
        if entry["maxTokens"] <= 0:
            failures.append(f"{entry['id']}: maxTokens must be positive")
        if entry["maxTokens"] > entry["contextWindow"]:
            failures.append(
                f"{entry['id']}: maxTokens {entry['maxTokens']} exceeds contextWindow {entry['contextWindow']}"
            )
        modalities = entry.get("input") or []
        if not modalities:
            failures.append(f"{entry['id']}: input must name at least one modality")
        unknown = [item for item in modalities if item not in ("text", "image")]
        if unknown:
            failures.append(f"{entry['id']}: unknown input modality {', '.join(map(str, unknown))}")
    for path, _, _, new_text in plans:
        if path.suffix in (".yml", ".yaml"):
            rebuilt, _ = rebuild_dsh_models(new_text.splitlines(keepends=True), entries)
            if "".join(rebuilt) != new_text:
                failures.append(f"{path.name}: the planned text does not round-trip through the rebuilder")
        else:
            rebuilt, _ = rebuild_opencode(new_text, entries)
            if rebuilt != new_text:
                failures.append(f"{path.name}: the planned text does not round-trip through the rebuilder")
    return failures


# ---------------------------------------------------------------- yml


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def rebuild_dsh_models(lines: list[str], entries: list[dict], provider: str = "um-genai") -> tuple[list[str], list[str]]:
    changes: list[str] = []
    out = list(lines)
    provider_index = None
    provider_indent = 0
    for index, line in enumerate(out):
        match = re.match(r"^(\s*)" + re.escape(provider) + r":\s*$", line)
        if match:
            provider_index, provider_indent = index, len(match.group(1))
            break
    if provider_index is None:
        raise SyncError(f"no '{provider}:' provider block found")

    models_index = None
    models_indent = 0
    models_inline_empty = False
    for index in range(provider_index + 1, len(out)):
        line = out[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = indent_of(line)
        if indent <= provider_indent:
            break
        # `models:` (block form) and `models: []` (what a YAML writer emits for an
        # empty list) both count; the inline form is rewritten to block form below.
        match = re.match(r"^(\s*)models:\s*(\[\s*\])?\s*$", line)
        if match:
            models_index, models_indent = index, indent
            models_inline_empty = bool(match.group(2))
            break
    if models_index is None:
        raise SyncError(f"the '{provider}:' provider has no 'models:' list")

    item_indent = models_indent + 2
    start = end = None
    index = models_index + 1
    while index < len(out):
        line = out[index]
        if line.strip():
            indent = indent_of(line)
            if start is None:
                if indent <= models_indent:
                    break  # empty list: splice here
                if indent != item_indent or not line.lstrip().startswith("- "):
                    break
                start = index
            elif indent <= models_indent:
                end = index
                break
        index += 1
    if end is None:
        end = len(out)
    if start is None:
        start = end = models_index + 1
    if models_inline_empty:
        out[models_index] = " " * models_indent + "models:\n"

    for line in out[start:end]:
        if line.strip().startswith("#"):
            raise SyncError("the models list contains a comment this script would delete; edit it by hand")

    padding = " " * item_indent
    rebuilt: list[str] = []
    for entry in entries:
        modalities = entry.get("input") or modalities_for(bool(entry.get("attachment", False)))
        rebuilt.append(f"{padding}- id: {entry['id']}\n")
        rebuilt.append(f"{padding}  name: {entry['display']}\n")
        rebuilt.append(f"{padding}  contextWindow: {entry['contextWindow']}\n")
        rebuilt.append(f"{padding}  maxTokens: {entry['maxTokens']}\n")
        rebuilt.append(f"{padding}  input: [{', '.join(modalities)}]\n")
    out[start:end] = rebuilt
    changes.append(f"rebuilt {len(entries)} model entries under '{provider}: models'")

    # every `provider: um-genai` + `model: <id>` pair (subagent allowed models) must follow the rename.
    cursor = 0
    pair = re.compile(r"^(\s*)(-\s+)?provider:\s*" + re.escape(provider) + r"\s*$")
    while cursor < len(out):
        match = pair.match(out[cursor])
        if match:
            # a mapping (`provider:` then `model:` at the same indent) or a list item
            # (`- provider:` then `model:` two columns further in).
            base_indent = len(match.group(1)) + (2 if match.group(2) else 0)
            look = cursor + 1
            while look < len(out):
                if not out[look].strip():
                    look += 1
                    continue
                if indent_of(out[look]) != base_indent:
                    break
                model_match = re.match(r"^(\s*)model:\s*(\S+)\s*$", out[look])
                if model_match:
                    current = model_match.group(2)
                    if current not in [entry["id"] for entry in entries]:
                        replacement = next(
                            (entry["id"] for entry in entries if family(entry["id"]) == family(current)),
                            None,
                        )
                        if replacement is None:
                            changes.append(f"warning: '{provider}:{current}' is not served upstream; left as is")
                        else:
                            out[look] = f"{model_match.group(1)}model: {replacement}\n"
                            changes.append(f"allowedModels: {current} -> {replacement}")
                    break
                look += 1
        cursor += 1
    return out, changes


# ---------------------------------------------------------------- opencode json


def rebuild_opencode(text: str, entries: list[dict], provider: str = "um-genai") -> tuple[str, list[str]]:
    changes: list[str] = []
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SyncError(f"opencode.json is not valid JSON: {exc}") from exc
    providers = config.get("provider", {})
    if provider not in providers:
        raise SyncError(f"opencode.json has no provider '{provider}'")
    models: dict[str, dict] = {}
    for entry in entries:
        models[entry["id"]] = {
            "name": entry["display"],
            "tool_call": entry["tool_call"],
            "temperature": entry["temperature"],
            "limit": {"context": entry["contextWindow"], "output": entry["maxTokens"]},
        }
        if entry.get("attachment"):
            models[entry["id"]]["attachment"] = True
    providers[provider]["models"] = models
    changes.append(f"rebuilt {len(entries)} provider.{provider}.models entries")

    ids = [entry["id"] for entry in entries]
    for key in ("model", "small_model"):
        value = config.get(key)
        if not isinstance(value, str):
            continue
        prefix, _, tail = value.partition("/")
        if not tail or prefix != provider:
            continue
        if tail in ids:
            continue
        replacement = next((entry["id"] for entry in entries if family(entry["id"]) == family(tail)), None)
        if replacement is None:
            changes.append(f"warning: {key} points at {tail!r} which upstream does not serve; left as is")
            continue
        config[key] = f"{provider}/{replacement}"
        changes.append(f"{key}: {value} -> {config[key]}")
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n", changes


# ---------------------------------------------------------------- files


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SyncError(f"cannot read {path}: {exc}") from exc


def backup(path: pathlib.Path) -> pathlib.Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-models-{stamp}-{uuid.uuid4().hex[:8]}")
    shutil.copy2(path, target)
    return target


def write_text(path: pathlib.Path, text: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".tmp-models-", dir=path.parent)
    temporary = pathlib.Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def config_lock(path: pathlib.Path):
    """Lock a persistent sidecar so atomic replacement cannot replace the lock."""
    try:
        handle = open(path.with_name(path.name + ".models-sync.lock"), "a+b")
    except OSError as exc:
        raise SyncError(f"cannot lock {path}; settings were not changed") from exc
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SyncError(f"another model sync owns {path}; retry later") from exc
        yield
    finally:
        handle.close()


def apply_plans(plans, log_file) -> None:
    """Lock all targets in a stable order before checking or changing inputs."""
    with ExitStack() as stack:
        for path in sorted({plan[0].resolve() for plan in plans}, key=str):
            stack.enter_context(config_lock(path))
        _apply_locked(plans, log_file)


def _apply_locked(plans, log_file) -> None:
    """Back up every input first; restore completed writes on partial failure."""
    for path, original, _, _ in plans:
        if read_text(path) != original:
            raise SyncError(f"{path} changed during sync; retry with the current settings")
    saved = [(plan, backup(plan[0])) for plan in plans]
    written = []
    try:
        for (path, original, _, new_text), saved_path in saved:
            if read_text(path) != original:
                raise SyncError(f"{path} changed during sync")
            write_text(path, new_text)
            written.append((path, original, new_text))
            log(f"rewrote {path} (backup: {saved_path.name})", log_file)
    except (OSError, SyncError) as exc:
        rollback_errors = []
        for path, original, new_text in reversed(written):
            try:
                if read_text(path) == new_text:
                    write_text(path, original)
                else:
                    rollback_errors.append(str(path))
            except (OSError, SyncError):
                rollback_errors.append(str(path))
        detail = f"; restore backups manually for: {', '.join(rollback_errors)}" if rollback_errors else "; completed writes rolled back"
        raise SyncError(f"model sync did not complete: {type(exc).__name__}{detail}") from exc


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="rewrite the configs")
    mode.add_argument("--auto", action="store_true", help="rewrite only when there is drift, stay quiet otherwise")
    mode.add_argument("--check", action="store_true", help="read-only drift check (this is already the default; the flag is accepted for scripts)")
    parser.add_argument("--target", default="both", choices=["both", "dsh", "opencode"])
    parser.add_argument("--profile", type=pathlib.Path, default=DEFAULT_PROFILE)
    parser.add_argument("--opencode", type=pathlib.Path, default=DEFAULT_OPENCODE)
    parser.add_argument("--registry", type=pathlib.Path, default=REGISTRY)
    parser.add_argument("--api-key-file", type=pathlib.Path, default=DEFAULT_KEY)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--log", type=pathlib.Path, default=DEFAULT_LOG)
    args = parser.parse_args()

    quiet = args.auto
    log_file = args.log if (args.apply or args.auto) else None
    registry = load_registry(args.registry)
    base_url = args.base_url or registry.get("provider", {}).get("baseURL", "http://127.0.0.1:8788/v1")
    api_key = read_api_key(args.api_key_file)

    try:
        catalog = fetch_catalog(base_url, api_key)
    except SyncError as exc:
        if args.auto:
            log(f"skipped: {exc}", log_file)
            return 0
        log(f"error: {exc}", log_file)
        return 2

    if CATALOG_SOURCE != "proxy":
        log("the proxy was asleep; compared against the UM gateway directly", log_file)

    warnings: list[str] = []
    entries = entries_for(catalog, registry, warnings)
    upstream = [entry["id"] for entry in entries]
    for warning in warnings:
        log(f"warning: {warning}", log_file)
    if not quiet:
        for line in report(entries, upstream).splitlines():
            log(f"catalog: {line}", log_file)

    plans: list[tuple[pathlib.Path, str, list[str], str]] = []
    if args.target in ("both", "dsh"):
        text = read_text(args.profile)
        lines = text.splitlines(keepends=True)
        new_lines, changes = rebuild_dsh_models(lines, entries)
        plans.append((args.profile, text, changes, "".join(new_lines)))
    if args.target in ("both", "opencode"):
        text = read_text(args.opencode)
        new_text, changes = rebuild_opencode(text, entries)
        plans.append((args.opencode, text, changes, new_text))

    failures = verify(entries, [item["id"] for item in catalog], plans)
    for failure in failures:
        log(f"verify: {failure}", log_file)
    if failures:
        log(f"verification failed ({len(failures)} problem(s)); nothing was written", log_file)
        return 5
    if not quiet:
        log(f"verified: {len(entries)} models match the gateway (id, name, contextWindow, maxTokens)", log_file)

    drift = [plan for plan in plans if plan[1] != plan[3]]
    if not drift:
        if not quiet:
            log(f"in sync: {', '.join(entry['id'] for entry in entries)}", log_file)
        return 0

    for path, _, changes, _ in drift:
        for change in changes:
            log(f"{path.name}: {change}", log_file)

    if not (args.apply or args.auto):
        log("drift detected; re-run with --apply to rewrite the configs", log_file)
        return 10

    apply_plans(drift, log_file)
    log("applied; restart the DSH host so the model picker reloads the new ids", log_file)
    return 20


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SyncError, OSError, ValueError, TypeError, AttributeError) as exc:
        try:
            print(f"error: {exc}", file=sys.stderr)
        except (AttributeError, OSError, ValueError):
            pass
        raise SystemExit(4)
