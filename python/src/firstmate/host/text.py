"""Reading a watcher close and the outcome store's rows, and composing what the host prints."""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from firstmate import files
from firstmate.processes import pid_from

WAKE = re.compile(r"(signal:|stale:|check:|heartbeat($|:))")
STATUS = re.compile(r"watcher: (started|attached) ")
VERIFIED = re.compile(r"watcher: (started|attached) pid=([0-9]+)")
_GENERATION = " recovery-generation="


@dataclass(frozen=True)
class Verified:
    """A successor cycle's live watcher, and the recovery generation it started
    (empty when the arm attached to a running watcher)."""

    watcher: int | None
    generation: str


def wake_lines(close: str) -> str:
    """The close's wake lines, the reason the host acts on, or empty."""
    return "\n".join(line for line in close.split("\n") if WAKE.match(line))


def first(text: str) -> str:
    return text.split("\n", 1)[0]


@dataclass(frozen=True)
class Close:
    """A closed watcher cycle: its output as the arm printed it, and the arm's pid."""

    text: str
    arm_pid: int | None

    @property
    def reason(self) -> str:
        """The wake lines the host acts on, or empty."""
        return wake_lines(self.text)

    @property
    def first_wake(self) -> str:
        return first(self.reason)


NO_CLOSE = Close("", None)


def status_line(complete_output: str) -> str:
    """The first cycle's status line among complete output lines, or empty."""
    for line in files.complete_lines(complete_output):
        if STATUS.match(line):
            return line
    return ""


def without_status(close: str, status: str) -> str:
    """The close without the first occurrence of an already printed status line."""
    lines = close.split("\n")
    if status in lines:
        lines.remove(status)
    return "\n".join(lines).rstrip("\n")


def verified_watcher(output: str) -> Verified | None:
    """The watcher an arm's first verified status line names, or None."""
    for line in output.split("\n"):
        match = VERIFIED.match(line)
        if match:
            generation = line.rsplit(_GENERATION, 1)[1] if _GENERATION in line else ""
            return Verified(pid_from(match.group(2)), generation)
    return None


def compose(close: str, lines: Iterable[str]) -> str:
    """The close followed by each non-empty line, as one text, or empty."""
    text = close
    for line in lines:
        if line:
            text = f"{text}\n{line}" if text else line
    return text


def unacknowledged(granted: Sequence[str], queued: str | None) -> tuple[str, ...]:
    """The granted rows the queue still holds. Every granted row when the queue
    could not be read (`queued` None)."""
    if queued is None:
        return tuple(granted)
    return tuple(line.split()[0] for line in queued.split("\n") if line.split())


def outcome_rows(text: str) -> list[Mapping[str, object]]:
    """Every row of an outcome-store lookup, as `jq -s` reads the stream.
    ValueError when one is malformed or not an object."""
    decoder = json.JSONDecoder()
    found: list[Mapping[str, object]] = []
    index = 0
    while True:
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        if index == len(text):
            return found
        row, index = decoder.raw_decode(text, index)
        if not isinstance(row, dict):
            raise ValueError("an outcome row is not an object")
        found.append(row)


def visible(rows: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """The rows the captain sees: every row not marked silent."""
    return [row for row in rows if row.get("silent") is not True]


def outcome_seqs(rows: Iterable[Mapping[str, object]]) -> str:
    """The rows' store sequence numbers, joined by ", "."""
    return ", ".join(_jq_text(row.get("seq")) for row in rows)


def outcome_lines(rows: Iterable[Mapping[str, object]]) -> str:
    """One "supervision-host: outcome" line per row."""
    lines = [
        f"supervision-host: outcome {_jq_text(row.get('seq'))} for {_jq_text(row.get('task'))} "
        f"[{_jq_text(row.get('verdict'))}]: {_jq_text(row.get('summary'))}"
        for row in rows
    ]
    return "\n".join(lines).replace("\r", "")


def _jq_text(value: object) -> str:
    """A value as jq's string interpolation renders it."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
