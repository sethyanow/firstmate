#!/usr/bin/env python3
"""Collect memory evidence before writing a brief (Python 3.11+).

Configuration: docs/configuration.md, "Brief memory checks" owns the schema for
FM_HOME/config/memory-check.json. Absent backends disable collection and gating.
Requests run concurrently, without retries or redirects.

run TASK --context FILE --query QUERY [--query QUERY ...] prints a check id.
data/TASK/memory-check.{json,md} contain the full exchange, queries, answers,
timestamps, durations, and errors. Immutable copies live in memory-checks/ID.*.
An error is evidence, not a successful recall; review errors before writing.

gate TASK --brief FILE enforces the opt-in config/memory-check-required flag
when backends are configured. The brief must contain exactly one unindented
"Memory check: ID" line matching this task's saved check. It checks provenance,
not recall quality, freshness, or whether the sender read the evidence.
FM_CONFIG_OVERRIDE and FM_DATA_OVERRIDE select the corresponding directories.

feedback TASK --run ID --flag ID --disposition acted-on|dropped --reason TEXT
appends a decision to data/TASK/brief-check-feedback.jsonl, after checking the
flag against brief-check/ID.json written by fm_brief_check.py. Repeated decisions
remain in the history; the last one is current. Pending flags have no decision.
"""

import argparse
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
CHECK_ID = re.compile(r"[a-f0-9]{32}")
MARKER = re.compile(r"^Memory check: (\S+)\s*$", re.MULTILINE)


def directory(home: Path, kind: str) -> Path:
    return Path(os.environ.get(f"FM_{kind.upper()}_OVERRIDE") or home / kind)


def task_directory(home: Path, task: str) -> Path:
    if not SLUG.fullmatch(task):
        raise ValueError("invalid task id")
    return directory(home, "data") / task


def load_backends(home: Path) -> list[dict[str, Any]]:
    path = directory(home, "config") / "memory-check.json"
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    if not isinstance(config, dict) or not isinstance(config.get("backends"), list):
        raise ValueError("memory-check.json must contain a backends array")
    names = set()
    for backend in config["backends"]:
        validate_backend(backend)
        name = backend["name"]
        if name in names:
            raise ValueError("backend names must be unique")
        names.add(name)
    return config["backends"]


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
    pointer = backend["answer_path"]
    if pointer and (not pointer.startswith("/") or re.search(r"~(?![01])", pointer)):
        raise ValueError(f"{name}: answer_path must be a JSON Pointer")
    timeout = backend.get("timeout_seconds", 60)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name}: timeout_seconds must be positive and finite")
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


def credentials(home: Path, auth: dict[str, str] | None) -> tuple[dict[str, str], str]:
    if auth is None:
        return {}, ""
    if "env" in auth:
        key = os.environ.get(auth["env"], "").strip()
    else:
        path = Path(auth["file"]).expanduser()
        if not path.is_absolute():
            path = directory(home, "config") / path
        key = path.read_text().strip()
    if not re.fullmatch(r"[\x21-\x7e]+", key):
        raise ValueError("missing or invalid authentication key")
    return {auth.get("header", "Authorization"): auth.get("prefix", "Bearer ") + key}, key


def expand_text(value: str, query: str, context: str) -> str:
    # Single substitution: placeholders inside supplied text stay literal.
    return re.sub(r"\{(query|context)\}", lambda m: query if m[1] == "query" else context, value)


def expand(value: object, query: str, context: str) -> object:
    if isinstance(value, str):
        return expand_text(value, query, context)
    if isinstance(value, list):
        return [expand(v, query, context) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, query, context) for k, v in value.items()}
    return value


def answer_at(value: object, pointer: str) -> object:
    if not pointer:
        return value
    for token in pointer[1:].split("/"):
        part = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError("answer_path does not resolve")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not forward credentials to a redirect target."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def redact(value: object, key: str) -> object:
    if isinstance(value, str):
        return value.replace(key, "[key]")
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, dict):
        return {name.replace(key, "[key]"): redact(item, key) for name, item in value.items()}
    return value


def recall(home: Path, backend: dict[str, Any], query: str, context: str) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "backend": backend["name"],
        "query": query,
        "started_at": datetime.now(UTC).isoformat(),
        "answer": None,
        "error": None,
    }
    key = ""
    try:
        headers, key = credentials(home, backend.get("auth"))
        headers["Content-Type"] = "application/json"
        body = expand(backend.get("body"), query, context)
        url = expand_text(
            backend["url"],
            urllib.parse.quote(query, safe=""),
            urllib.parse.quote(context, safe=""),
        )
        request = urllib.request.Request(
            url,
            method=backend["method"],
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        with urllib.request.build_opener(NoRedirect).open(
            request, timeout=backend.get("timeout_seconds", 60)
        ) as response:
            result["answer"] = answer_at(json.load(response), backend["answer_path"])
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode(errors="replace")
        except (OSError, http.client.HTTPException):
            detail = "unreadable error response"
        finally:
            error.close()
        if key:
            detail = detail.replace(key, "[key]")
            detail = detail.replace(json.dumps(key)[1:-1], "[key]")
        result["error"] = f"HTTP {error.code}: {detail[:2000]}"
    except (OSError, ValueError, KeyError, IndexError, http.client.HTTPException) as error:
        # Transport exceptions can echo credentials; retain the category only.
        result["error"] = type(error).__name__
    result["seconds"] = round(time.monotonic() - started, 3)
    if key:
        result["answer"] = redact(result["answer"], key)
    return result


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    with part.open("x", encoding="utf-8") as file:
        file.write(value)
    part.replace(path)


def render(record: dict[str, Any]) -> str:
    lines = [
        f"# Memory check {record['id']}",
        f"Task: {record['task']}",
        f"Created: {record['created_at']}",
        "",
        "## Exchange",
        record["context"],
    ]
    for result in record["results"]:
        answer = result["answer"]
        lines.extend(
            [
                "",
                f"## {result['backend']}",
                f"Query: {result['query']}",
                f"Started: {result['started_at']}",
                f"Seconds: {result['seconds']}",
                f"Error: {result['error'] or 'none'}",
                "",
                answer
                if isinstance(answer, str)
                else json.dumps(answer, indent=2, ensure_ascii=False),
            ]
        )
    return "\n".join(lines) + "\n"


def run(home: Path, task: str, context_file: Path, queries: list[str]) -> str | None:
    folder = task_directory(home, task)
    backends = load_backends(home)
    if not backends:
        return None
    context = context_file.read_text()
    if not context.strip() or any(not q.strip() for q in queries):
        raise ValueError("context and queries must not be empty")
    record: dict[str, Any] = {
        "version": 1,
        "id": uuid.uuid4().hex,
        "task": task,
        "created_at": datetime.now(UTC).isoformat(),
        "context": context,
        "context_file": str(context_file.resolve()),
        "queries": queries,
    }
    with ThreadPoolExecutor(max_workers=len(queries) * len(backends)) as pool:
        futures = [
            pool.submit(recall, home, backend, query, context)
            for query in queries
            for backend in backends
        ]
        record["results"] = [future.result() for future in futures]
    markdown = render(record)
    for stem in (folder / "memory-checks" / record["id"], folder / "memory-check"):
        write_text(stem.with_suffix(".md"), markdown)
        write_json(stem.with_suffix(".json"), record)
    failed = sum(result["error"] is not None for result in record["results"])
    if failed:
        sys.stderr.write(
            f"memory-check: {failed} request(s) failed; review {folder / 'memory-check.md'}\n"
        )
    return record["id"]


def valid_results(results: object) -> bool:
    return (
        isinstance(results, list)
        and bool(results)
        and all(
            isinstance(result, dict)
            and {"answer", "error", "query", "backend", "seconds", "started_at"} <= result.keys()
            and isinstance(result["query"], str)
            and isinstance(result["backend"], str)
            and isinstance(result["seconds"], (int, float))
            and math.isfinite(result["seconds"])
            and result["seconds"] >= 0
            and (result["error"] is None or isinstance(result["error"], str))
            for result in results
        )
    )


def read_check(home: Path, task: str, text: str = "") -> dict[str, Any] | None:
    folder = task_directory(home, task)
    ids = MARKER.findall(text)
    if len(ids) > 1 or (ids and not CHECK_ID.fullmatch(ids[0])):
        raise ValueError("brief must contain one valid Memory check: ID line")
    path = folder / "memory-checks" / f"{ids[0]}.json" if ids else folder / "memory-check.json"
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        if not ids:
            return None
        raise ValueError("memory check id has no saved check for this task") from None
    if (
        not isinstance(record, dict)
        or record.get("version") != 1
        or record.get("task") != task
        or not CHECK_ID.fullmatch(str(record.get("id", "")))
        or (ids and record["id"] != ids[0])
        or not isinstance(record.get("context"), str)
        or not record["context"].strip()
        or not valid_results(record.get("results"))
    ):
        raise ValueError("invalid memory check record for this task")
    return record


def gate(home: Path, task: str, brief: Path) -> None:
    if not (directory(home, "config") / "memory-check-required").exists() or not load_backends(
        home
    ):
        return
    text = brief.read_text()
    if len(MARKER.findall(text)) != 1:
        raise ValueError("memory check required: add Memory check: ID to the brief")
    read_check(home, task, text)


def save_flags(home: Path, task: str, run_record: dict[str, Any]) -> str:
    run_id = run_record["run_id"]
    path = task_directory(home, task) / "brief-check" / f"{run_id}.json"
    write_json(
        path,
        {
            "run_id": run_id,
            "task": task,
            "log_path": run_record["log_path"],
            "memory_check_id": run_record.get("memory_check_id"),
            "flags": run_record["flags"],
        },
    )
    return str(path)


def feedback(home: Path, args: argparse.Namespace) -> None:
    folder = task_directory(home, args.task)
    if not CHECK_ID.fullmatch(args.run):
        raise ValueError("invalid brief-check run id")
    run_record = json.loads((folder / "brief-check" / f"{args.run}.json").read_text())
    if run_record["task"] != args.task or run_record["run_id"] != args.run:
        raise ValueError("brief-check run belongs to another task")
    if args.flag not in {flag["id"] for flag in run_record["flags"]}:
        raise ValueError("unknown flag in this brief-check run")
    if not args.reason.strip():
        raise ValueError("feedback needs a reason")
    record = {
        "run_id": args.run,
        "flag_id": args.flag,
        "disposition": args.disposition,
        "reason": args.reason,
        "at": datetime.now(UTC).isoformat(),
    }
    with (folder / "brief-check-feedback.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("run", help="query configured backends and print a check id")
    collect.add_argument("task")
    collect.add_argument("--context", type=Path, required=True)
    collect.add_argument("--query", action="append", required=True)
    check = commands.add_parser("gate", help="validate a fresh launch when the gate is enabled")
    check.add_argument("task")
    check.add_argument("--brief", type=Path, required=True)
    followup = commands.add_parser("feedback", help="record how one brief-check flag was handled")
    followup.add_argument("task")
    followup.add_argument("--run", required=True)
    followup.add_argument("--flag", required=True)
    followup.add_argument("--disposition", choices=("acted-on", "dropped"), required=True)
    followup.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        home = Path(os.environ["FM_HOME"])
        if args.command == "run":
            check_id = run(home, args.task, args.context, args.query)
            if check_id:
                sys.stdout.write(check_id + "\n")
            else:
                sys.stderr.write("memory-check: disabled (no backends configured)\n")
        elif args.command == "gate":
            gate(home, args.task, args.brief)
        else:
            feedback(home, args)
    except (OSError, ValueError, KeyError) as error:
        sys.stderr.write(f"memory-check: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
