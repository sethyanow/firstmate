"""Loading and validating FM_HOME/config/memory-check.json.

docs/configuration.md, "Brief memory checks", owns the schema.
"""

import json
import math
import os
import re
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SOURCE_KIND = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
SOURCE_TIMEOUT = 10
REQUEST_TIMEOUT = 300
PLACEHOLDER = re.compile(r"\{(query|context|level)\}")


def directory(home: Path, kind: str) -> Path:
    return Path(os.environ.get(f"FM_{kind.upper()}_OVERRIDE") or home / kind)


def load_config(home: Path) -> dict[str, Any]:
    path = directory(home, "config") / "memory-check.json"
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError:
        return {"backends": [], "quote_sources": {}}
    if not isinstance(config, dict) or not isinstance(config.get("backends"), list):
        raise ValueError("memory-check.json must contain a backends array")
    names = set()
    for backend in config["backends"]:
        validate_backend(backend)
        name = backend["name"]
        if name in names:
            raise ValueError("backend names must be unique")
        names.add(name)
    sources = config.setdefault("quote_sources", {})
    if not isinstance(sources, dict):
        raise ValueError("quote_sources must be an object")
    for kind, source in sources.items():
        validate_source(kind, source)
    return config


def load_backends(home: Path) -> list[dict[str, Any]]:
    backends = load_config(home)["backends"]
    for backend in backends:
        validate_levels(backend)
    shared_levels(backends)
    return backends


def shared_levels(backends: list[dict[str, Any]]) -> set[str]:
    level_names = [set(levels) for backend in backends if (levels := backend.get("levels"))]
    if not level_names:
        return set()
    names = level_names[0].intersection(*level_names[1:])
    if not names:
        raise ValueError("backends with levels must share at least one level name")
    return names


def validate_source(kind: str, source: object) -> None:
    if not SOURCE_KIND.fullmatch(kind):
        raise ValueError("quote source names must be slugs that start with a letter")
    if not isinstance(source, dict) or set(source) - {"command", "timeout_seconds"}:
        raise ValueError(f"{kind}: a quote source has a command and timeout_seconds only")
    command = source.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(a, str) for a in command)
    ):
        raise ValueError(f"{kind}: command must be a non-empty array of strings")
    validate_timeout(kind, source.get("timeout_seconds", SOURCE_TIMEOUT))


def validate_timeout(name: str, timeout: object) -> None:
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name}: timeout_seconds must be positive and finite")


def validate_backend(backend: object) -> None:
    if not isinstance(backend, dict):
        raise ValueError("each backend must be an object")
    for key in ("name", "url", "method", "answer_path"):
        if not isinstance(backend.get(key), str):
            raise ValueError(f"each backend needs a string {key}")
    name = backend["name"]
    if not SLUG.fullmatch(name):
        raise ValueError("backend names must be slugs")
    url = urllib.parse.urlsplit(backend["url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError(f"{name}: use an HTTP(S) URL without credentials")
    if not re.fullmatch(r"[A-Z]+", backend["method"]):
        raise ValueError(f"{name}: method must be an uppercase HTTP method")
    for field in ("answer_path", "item_path"):
        pointer = backend.get(field, "")
        if not isinstance(pointer, str) or (
            pointer and (not pointer.startswith("/") or re.search(r"~(?![01])", pointer))
        ):
            raise ValueError(f"{name}: {field} must be a JSON Pointer")
    validate_timeout(name, backend.get("timeout_seconds", REQUEST_TIMEOUT))
    validate_auth(backend.get("auth"))


def map_strings(value: object, change: Callable[[str], str]) -> object:
    """A copy of a JSON value with change applied to every string value, keys excepted."""
    if isinstance(value, str):
        return change(value)
    if isinstance(value, list):
        return [map_strings(item, change) for item in value]
    if isinstance(value, dict):
        return {key: map_strings(item, change) for key, item in value.items()}
    return value


def placeholders(value: object) -> set[str]:
    """Placeholder names that string values anywhere in a JSON value use."""
    names: set[str] = set()

    def note(text: str) -> str:
        names.update(PLACEHOLDER.findall(text))
        return text

    map_strings(value, note)
    return names


def validate_levels(backend: dict[str, Any]) -> None:
    name = backend["name"]
    levels = backend.get("levels")
    url = urllib.parse.urldefrag(backend["url"]).url
    uses_level = "level" in placeholders([url, backend.get("body")])
    if levels is None:
        if uses_level:
            raise ValueError(f"{name}: the {{level}} placeholder needs a levels object")
        return
    if not uses_level:
        raise ValueError(f"{name}: levels needs a {{level}} placeholder in url or body")
    if (
        not isinstance(levels, dict)
        or not levels
        or not all(SLUG.fullmatch(level) for level in levels)
        or not all(isinstance(spelling, str) and spelling for spelling in levels.values())
    ):
        raise ValueError(f"{name}: levels maps level names to non-empty request values")


def resolve_levels(backends: list[dict[str, Any]], level: str | None) -> dict[str, str]:
    choices = shared_levels(backends)
    if not choices:
        if level is not None:
            raise ValueError("--level requires at least one backend with levels")
        return {}
    if level is None or level not in choices:
        wanted = "--level is required" if level is None else f"unknown level {level!r}"
        raise ValueError(f"{wanted} (configured: {', '.join(sorted(choices))})")
    return {
        backend["name"]: levels[level]
        for backend in backends
        if (levels := backend.get("levels"))
    }


def validate_auth(auth: object) -> None:
    if auth is None:
        return
    if not isinstance(auth, dict) or set(auth) - {"env", "file", "header", "prefix"}:
        raise ValueError("auth supports env or file, header and prefix only")
    if ("env" in auth) == ("file" in auth):
        raise ValueError("auth requires exactly one key source: env or file")
    if not all(isinstance(v, str) for v in auth.values()):
        raise ValueError("auth values must be strings")
    if not auth.get("env", auth.get("file")):
        raise ValueError("auth key source must not be empty")
    if not re.fullmatch(r"[A-Za-z0-9-]+", auth.get("header", "Authorization")):
        raise ValueError("invalid auth header name")
    if not re.fullmatch(r"[\x20-\x7e]*", auth.get("prefix", "Bearer ")):
        raise ValueError("invalid auth prefix")
