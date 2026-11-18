"""Loading and validating FM_HOME/config/memory-check.json.

docs/configuration.md, "Brief memory checks", owns the schema.
"""

import json
import math
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SOURCE_KIND = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*")
SOURCE_TIMEOUT = 10


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
    return load_config(home)["backends"]


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
    validate_timeout(name, backend.get("timeout_seconds", 60))
    validate_auth(backend.get("auth"))


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
