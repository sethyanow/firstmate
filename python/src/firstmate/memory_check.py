#!/usr/bin/env python3
# long-module: Keep saved-evidence creation, rendering, validation, and the CLI together.
"""Ask memory questions or collect evidence before writing a brief (Python 3.11+).

Configuration: docs/configuration.md, "Brief memory checks" owns the schema for
FM_HOME/config/memory-check.json. The helper sends every query at once to each
backend the command asks, without retries or redirects.

LEVELS: --level LEVEL sets one recall level for every query in a command.
Supply --level when any backend the command asks declares levels and omit it
otherwise. Choose a name shared by those backends that declare levels. If the
level is missing or unknown, the command lists the shared names and stops
before sending requests. Backends without levels keep their request templates.

ask [--level LEVEL] --query QUERY [--query QUERY ...] asks every backend and
prints one Markdown report.
remember asks only the backend named honcho. facts asks only the backend named
hindsight. Both take the same options as ask and require their named backend.
The header shows successful nonempty answers out of all requests, query and
backend counts, the selected level and where it applies, and total seconds.
Each "## qN: QUERY" section lists every backend asked in configuration order
under "### BACKEND, SECONDS s" headings. The block quotes the answer, "Error: ..."
for a failed request, or "(no memory found)" when the answer is empty.
Hindsight results print one "- LABELS | TEXT" line per fact with a date, type,
and "s:" followed by the first 8 characters of the session ID when available.
The date comes from occurred_start, occurred_end, or mentioned_at, in that order.
Line breaks become spaces, and identical texts print once per question.
An unexpected Hindsight answer shape prints an error for that question.
Other backends' answers print as text or indented JSON.
These commands supply an empty context, write no files or check id, and bypass
the gate.

run TASK [--level LEVEL] --context FILE --query QUERY [--query QUERY ...] prints
a check id. data/TASK/memory-check.{json,md} contain the full exchange, queries,
answers, timestamps, durations, errors, and the selected level and where it
applies. The JSON fields are level (null when unset) and level_backends (the list
of affected backend names). A backend's item_path values go in its results'
items, with null for an item that lacks one.
Immutable copies live in memory-checks/ID.*.
An error is evidence, not a successful recall; review errors before writing.
In the exchange file, put double quotes only around his own words. A quote that
comes from somewhere other than his chat messages follows a [KIND:ID] tag on its
line, where KIND names one of the configured quote_sources; the brief check reads
that source for the ID and checks the quote against it.

gate TASK --brief FILE enforces the opt-in config/memory-check-required flag
when backends are configured. The brief must contain exactly one unindented
"Memory check: ID" line matching this task's saved check. It checks provenance,
not recall quality, freshness, or whether the sender read the evidence.
It checks saved records independently of current recall levels and accepts
valid records from before level metadata was added.
FM_CONFIG_OVERRIDE and FM_DATA_OVERRIDE select the corresponding directories.

The compatibility feedback subcommand delegates to fm_brief_check.py feedback;
that command owns flag records and their dispositions.
"""

import argparse
import http.client
import json
import math
import os
import re
import subprocess
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

from . import files
from .memory_config import (
    PLACEHOLDER,
    REQUEST_TIMEOUT,
    SLUG,
    SOURCE_KIND,
    SOURCE_TIMEOUT,
    directory,
    load_backends,
    load_config,
    map_strings,
    resolve_levels,
)

# bin/fm-pr-lib.sh: fm_task_id_creation_valid owns the task-id rule.
TASK_ID = re.compile(r"(?!\.)[A-Za-z0-9._-]{1,64}")
SOURCE_TAG = re.compile(rf"\[({SOURCE_KIND.pattern}):({SLUG.pattern})\]")
CHECK_ID = re.compile(r"[a-f0-9]{32}")
MARKER = re.compile(r"^Memory check: (\S+)\s*$", re.MULTILINE)
HINDSIGHT = "hindsight"
RECALL_BACKENDS = {"remember": "honcho", "facts": HINDSIGHT}
FACT_LINE_BREAKS = re.compile(r"\r\n|[\n\r\x85\u2028\u2029]")
TERMINAL_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def task_directory(home: Path, task: str) -> Path:
    if not TASK_ID.fullmatch(task):
        raise ValueError("invalid task id")
    return directory(home, "data") / task


def source_text(home: Path, kind: str, source_id: str, limit: float) -> str:
    """The owner's messages that the configured quote source prints for one id."""
    source = load_config(home)["quote_sources"].get(kind)
    if source is None:
        raise ValueError(f"no quote source {kind} in memory-check.json")
    if not SLUG.fullmatch(source_id):
        raise ValueError(f"invalid {kind} id")
    command = [arg.replace("{id}", source_id) for arg in source["command"]]
    env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
    try:
        done = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=min(limit, source.get("timeout_seconds", SOURCE_TIMEOUT)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"{kind}:{source_id} timed out") from None
    if done.returncode != 0:
        raise ValueError(f"{kind}:{source_id} exited {done.returncode}")
    return done.stdout


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


def expand_text(value: str, values: dict[str, str]) -> str:
    # Single substitution: placeholders inside supplied text stay literal.
    return PLACEHOLDER.sub(lambda m: values.get(m[1], m[0]), value)


def expand(value: object, values: dict[str, str]) -> object:
    return map_strings(value, lambda text: expand_text(text, values))


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


def item_at(item: object, pointer: str) -> object:
    """One list item's value, or None when this item lacks it."""
    try:
        return answer_at(item, pointer)
    except (ValueError, KeyError, IndexError):
        return None


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


def recall(
    home: Path, backend: dict[str, Any], values: dict[str, str], *, ad_hoc: bool
) -> dict[str, Any]:
    """One request with values filled into its placeholders: query, context, and level."""
    started = time.monotonic()
    result: dict[str, Any] = {
        "backend": backend["name"],
        "query": values["query"],
        "started_at": datetime.now(UTC).isoformat(),
        "answer": None,
        "error": None,
    }
    key = ""
    try:
        headers, key = credentials(home, backend.get("auth"))
        headers["Content-Type"] = "application/json"
        body = expand(backend.get("body"), values)
        url = expand_text(
            urllib.parse.urldefrag(backend["url"]).url,
            {k: urllib.parse.quote(v, safe="") for k, v in values.items()},
        )
        request = urllib.request.Request(
            url,
            method=backend["method"],
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        with urllib.request.build_opener(NoRedirect).open(
            request, timeout=backend.get("timeout_seconds", REQUEST_TIMEOUT)
        ) as response:
            answer = answer_at(json.load(response), backend["answer_path"])
        result.update(answer_fields(answer, backend, key, ad_hoc=ad_hoc))
    except urllib.error.HTTPError as error:
        try:
            detail = error.read(8192).decode(errors="replace")
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
    return result


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not files.replace(str(path), files.encode(value)):
        raise OSError(f"could not write {path}")


def as_text(answer: object) -> str:
    return answer if isinstance(answer, str) else json.dumps(answer, indent=2, ensure_ascii=False)


def level_name(level: str | None, level_backends: list[str]) -> str:
    if level is None:
        return "unset"
    return f"{level} (applies to {', '.join(level_backends)})"


def report_lines(text: str) -> list[str]:
    lines = re.split(r"\r\n?|\n", text)
    return lines if lines[-1] else lines[:-1]


def quoted(lines: list[str]) -> str:
    """Lines as a Markdown quote, so headings inside it cannot pass for report headings."""
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def quoted_result(text: str) -> str:
    return quoted(
        [
            TERMINAL_CONTROLS.sub(lambda match: f"\\u{ord(match[0]):04x}", line)
            for line in report_lines(text)
        ]
    )


def labeled_text(label: str, text: str) -> str:
    first, *continuation = report_lines(text) or [""]
    lead = f"{label} {first}"
    return f"{lead}\n{quoted(continuation)}" if continuation else lead


def render(record: dict[str, Any]) -> str:
    lines = [
        f"# Memory check {record['id']}",
        f"Task: {record['task']}",
        f"Created: {record['created_at']}",
        f"Level: {level_name(record['level'], record['level_backends'])}",
        "",
        "## Exchange",
        record["context"],
    ]
    for result in record["results"]:
        lines.extend(
            [
                "",
                f"## {result['backend']}",
                f"Query: {result['query']}",
                f"Started: {result['started_at']}",
                f"Seconds: {result['seconds']}",
                f"Error: {result['error'] or 'none'}",
                "",
                as_text(result["answer"]),
            ]
        )
    return "\n".join(lines) + "\n"


def collect(
    home: Path,
    backends: list[dict[str, Any]],
    queries: list[str],
    context: str,
    spellings: dict[str, str],
    *,
    ad_hoc: bool = False,
) -> list[list[dict[str, Any]]]:
    """Each query's results, one per backend in configuration order."""
    if not queries or any(not query.strip() for query in queries):
        raise ValueError("queries must not be empty")
    if not backends:
        return []
    with ThreadPoolExecutor(max_workers=len(queries) * len(backends)) as pool:
        futures = [
            [
                pool.submit(
                    recall,
                    home,
                    backend,
                    {"query": query, "context": context}
                    | (
                        {"level": spellings[backend["name"]]}
                        if backend["name"] in spellings
                        else {}
                    ),
                    ad_hoc=ad_hoc,
                )
                for backend in backends
            ]
            for query in queries
        ]
        return [[future.result() for future in row] for row in futures]


def label_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def fact_lines(items: object, *, key: str = "") -> list[str]:
    """Format Hindsight recall results, deduplicating the original text in one answer."""
    if not isinstance(items, list):
        raise ValueError("unexpected Hindsight answer: expected a results array")
    seen: set[str] = set()
    lines = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(text := item.get("text"), str):
            raise ValueError("unexpected Hindsight fact: expected an object with string text")
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        date = (
            label_text(item.get("occurred_start"))
            or label_text(item.get("occurred_end"))
            or label_text(item.get("mentioned_at"))
        )
        kind = label_text(item.get("type"))
        session = label_text(item_at(item, "/metadata/session_id"))
        if key:
            text, date, kind, session = (
                value.replace(key, "[key]") for value in (text, date, kind, session)
            )
        labels = " ".join(
            label
            for label in (date[:10], kind, session and f"s:{session[:8]}")
            if label
        )
        line = f"- {labels} | {text}" if labels else f"- {text}"
        lines.append(FACT_LINE_BREAKS.sub(" ", line))
    return lines


def answer_fields(
    answer: object, backend: dict[str, Any], key: str, *, ad_hoc: bool
) -> dict[str, object]:
    if ad_hoc and backend["name"] == HINDSIGHT:
        if not backend["answer_path"] and isinstance(answer, dict):
            answer = answer.get("results")
        try:
            return {"answer": "\n".join(fact_lines(answer, key=key))}
        except ValueError as error:
            return {"error": str(error)}
    fields = {"answer": answer}
    if backend.get("item_path") and isinstance(answer, list):
        fields["items"] = [item_at(item, backend["item_path"]) for item in answer]
    return {name: redact(value, key) for name, value in fields.items()} if key else fields


def answer_text(result: dict[str, Any]) -> tuple[str, bool]:
    """One result's report text, and whether it holds memory."""
    if result["error"] is not None:
        return f"Error: {result['error']}", False
    answer = result["answer"]
    if answer in (None, [], {}) or (isinstance(answer, str) and not answer.strip()):
        memory = ""
    else:
        memory = as_text(answer)
    return memory or "(no memory found)", bool(memory)


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def ask_report(
    queries: list[str],
    results: list[list[dict[str, Any]]],
    level: str,
    seconds: float,
) -> str:
    """The report for each query's results, one per backend, as collect returns them.

    level is the header's level text, as level_name gives it.
    """
    rows = [[(result, *answer_text(result)) for result in row] for row in results]
    found = sum(has_memory for row in rows for _result, _text, has_memory in row)
    backend_count = len(results[0])
    blocks = [
        f"recalled {found} of {plural(len(queries) * backend_count, 'answer')}: "
        f"{plural(len(queries), 'question')} x {plural(backend_count, 'backend')}, "
        f"level {level}, {seconds:.1f} s"
    ]
    for number, (query, row) in enumerate(zip(queries, rows, strict=True), 1):
        blocks.append(labeled_text(f"## q{number}:", query))
        blocks.extend(
            f"### {result['backend']}, {result['seconds']:.1f} s\n{quoted_result(text)}"
            for result, text, _has_memory in row
        )
    return "\n\n".join(blocks)


def recall_report(home: Path, command: str, queries: list[str], level: str | None) -> str:
    """The report of ask, remember, or facts: ask asks every backend."""
    started = time.monotonic()
    backends = load_backends(home)
    if name := RECALL_BACKENDS.get(command):
        backends = [backend for backend in backends if backend["name"] == name]
        if not backends:
            raise ValueError(f"{command} needs a backend named {name} in memory-check.json")
    spellings = resolve_levels(backends, level)
    results = collect(home, backends, queries, "", spellings, ad_hoc=True)
    if not results:
        return ""
    return ask_report(
        queries,
        results,
        level_name(level, list(spellings)),
        time.monotonic() - started,
    )


def run(
    home: Path, task: str, context_file: Path, queries: list[str], level: str | None
) -> str | None:
    folder = task_directory(home, task)
    backends = load_backends(home)
    spellings = resolve_levels(backends, level)
    if not backends:
        return None
    context = context_file.read_text()
    if not context.strip():
        raise ValueError("context must not be empty")
    record: dict[str, Any] = {
        "version": 1,
        "id": uuid.uuid4().hex,
        "task": task,
        "created_at": datetime.now(UTC).isoformat(),
        "context": context,
        "context_file": str(context_file.resolve()),
        "queries": queries,
        "level": level,
        "level_backends": list(spellings),
        "results": [
            result
            for row in collect(home, backends, queries, context, spellings)
            for result in row
        ],
    }
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
    if (
        not (directory(home, "config") / "memory-check-required").exists()
        or not load_config(home)["backends"]
    ):
        return
    text = brief.read_text()
    if len(MARKER.findall(text)) != 1:
        raise ValueError("memory check required: add Memory check: ID to the brief")
    read_check(home, task, text)


def main() -> int:
    if sys.argv[1:2] == ["feedback"]:
        from .brief_check_records import feedback_command

        return feedback_command(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    asks = [
        commands.add_parser("ask", help="print every backend's answers without saving a check"),
        commands.add_parser("remember", help="print answers from the honcho backend"),
        commands.add_parser("facts", help="print fact lines from the hindsight backend"),
    ]
    collect = commands.add_parser("run", help="query configured backends and print a check id")
    collect.add_argument("task")
    collect.add_argument("--context", type=Path, required=True)
    for recall_command in (*asks, collect):
        recall_command.add_argument("--query", action="append", required=True)
        recall_command.add_argument(
            "--level", help="recall level for every query; LEVELS in the main --help explains it"
        )
    check = commands.add_parser("gate", help="validate a fresh launch when the gate is enabled")
    check.add_argument("task")
    check.add_argument("--brief", type=Path, required=True)
    commands.add_parser("feedback", help="alias for fm_brief_check.py feedback")
    args = parser.parse_args()
    try:
        home = Path(os.environ["FM_HOME"])
        if args.command == "gate":
            gate(home, args.task, args.brief)
        else:
            output = (
                run(home, args.task, args.context, args.query, args.level)
                if args.command == "run"
                else recall_report(home, args.command, args.query, args.level)
            )
            if output:
                sys.stdout.write(output + "\n")
            else:
                sys.stderr.write("memory-check: disabled (no backends configured)\n")
    except (OSError, ValueError, KeyError) as error:
        sys.stderr.write(f"memory-check: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
