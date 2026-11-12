"""Reading a watcher close and composing what the host prints."""

import json
import re

WAKE = re.compile(r"(signal:|stale:|check:|heartbeat($|:))")
STATUS = re.compile(r"watcher: (started|attached) ")
VERIFIED = re.compile(r"watcher: (started|attached) pid=([0-9]+)")


def wake_lines(close):
    """The close's wake lines, the reason the host acts on, or empty."""
    return "\n".join(line for line in close.split("\n") if WAKE.match(line))


def first(text):
    return text.split("\n", 1)[0]


def status_line(complete_output):
    """The first cycle's status line among complete output lines, or empty."""
    for line in complete_output.split("\n")[:-1]:
        if STATUS.match(line):
            return line
    return ""


def without_status(close, status):
    """The close without the first occurrence of an already printed status line."""
    lines = close.split("\n")
    if status in lines:
        lines.remove(status)
    return "\n".join(lines).rstrip("\n")


def verified_watcher(output):
    """(watcher pid, recovery generation) from an arm's first verified status line, or None."""
    for line in output.split("\n"):
        match = VERIFIED.match(line)
        if match:
            marker = " recovery-generation="
            generation = line.rsplit(marker, 1)[1] if marker in line else ""
            return match.group(2), generation
    return None


def compose(close, lines):
    """The close followed by each non-empty line, as one text, or empty."""
    text = close
    for line in lines:
        if line:
            text = f"{text}\n{line}" if text else line
    return text


def turn_failure(rc, timed_out, usage, receipts, unacked, turn_timeout, engine):
    """Why a turn that did not handle its wake left it for main."""
    if timed_out:
        return f"the engine turn hit its {turn_timeout}s bound"
    if rc == 127:
        return f"the {engine} engine could not run"
    if rc != 0:
        return f"the engine turn failed (exit {rc})"
    if not usage.startswith("error=0"):
        return "the engine turn ended with an error or an incomplete result"
    if receipts == 0:
        return "the engine turn recorded no outcome for its wake"
    return f"the engine turn left its granted wake rows {unacked} unacknowledged"


def unacknowledged(granted, queued):
    """The granted rows the queue still holds, space-separated; every granted
    row when the queue could not be read (`queued` None)."""
    if queued is None:
        return " ".join(granted.split())
    return " ".join(line.split()[0] for line in queued.split("\n") if line.split())


def json_values(text):
    """Every JSON value in a stream, as `jq -s` reads it; ValueError when one is malformed."""
    decoder = json.JSONDecoder()
    values = []
    index = 0
    while True:
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        if index == len(text):
            return values
        value, index = decoder.raw_decode(text, index)
        values.append(value)


def visible_seqs(rows):
    """The store rows of the visible outcomes, joined by ", "; ValueError on a malformed row."""
    seqs = []
    for row in json_values(rows):
        if not isinstance(row, dict):
            raise ValueError("an outcome row is not an object")
        if row.get("silent") is not True:
            seqs.append(_jq_text(row.get("seq")))
    return ", ".join(seqs)


def outcome_lines(rows):
    """One "supervision-host: outcome" line per visible outcome."""
    lines = []
    for row in json_values(rows):
        if isinstance(row, dict) and row.get("silent") is not True:
            lines.append(f"supervision-host: outcome {_jq_text(row.get('seq'))} for {_jq_text(row.get('task'))} "
                         f"[{_jq_text(row.get('verdict'))}]: {_jq_text(row.get('summary'))}")
    return "\n".join(lines).replace("\r", "")


def _jq_text(value):
    """A value as jq's string interpolation renders it."""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
