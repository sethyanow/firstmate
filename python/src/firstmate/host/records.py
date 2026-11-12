"""The host's process record, its ledger, and its pid-and-identity records.

The process record (state/.supervision-host) holds one `role<TAB>pid<TAB>identity`
row per process the host runs, so a successor host stops exactly those.
The ledger (state/.supervision-host.log) holds one `epoch<TAB>text` row per event
and keeps its last 400 rows once it passes 600.
A pid-and-identity record holds one `pid<TAB>identity` line: the engine turn's
bounded process, the successor a hand-back left for main, and the acknowledged
hand-back owner.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from firstmate import files
from firstmate.processes import pid_from
from firstmate.signals import Stops

LEDGER_LIMIT = 600
LEDGER_KEEP = 400


@dataclass(frozen=True)
class ProcessRow:
    role: str
    pid: int | None
    identity: str


@dataclass(frozen=True)
class PidIdentity:
    pid: int | None
    identity: str


def rows(path: str) -> list[ProcessRow]:
    """Every complete row of the process record."""
    found = []
    for line in files.complete_lines(files.read_text(path) or ""):
        role, pid, identity = files.tsv(line, 3)
        found.append(ProcessRow(role, pid_from(pid), identity))
    return found


def append(path: str, role: str, pid: int, identity: str) -> None:
    try:
        with open(path, "ab") as handle:
            handle.write(files.encode(f"{role}\t{pid}\t{identity}\n"))
    except OSError:
        # The row only helps a successor host stop this process: the host's
        # own cleanup stops it without the row.
        return


def write_host(path: str, pid: int, identity: str) -> bool:
    """Start the record with this host's row, replacing any predecessor's."""
    return files.write(path, f"host\t{pid}\t{identity}\n")


def forget(path: str, pid: int) -> None:
    """Drop every row of `pid`."""
    if os.path.isfile(path):
        _rewrite(path, lambda line: None if _pid_of(line) == pid else line)


def refresh(path: str, pid: int, identity: str) -> None:
    """Record `pid` under `identity` when a row holds another identity for it."""
    if not identity or not os.path.isfile(path):
        return
    found = [files.tsv(line, 3) for line in files.lines(files.read_text(path) or "")]
    if not any(pid_from(row[1]) == pid and row[2] != identity for row in found):
        return

    def update(line: str) -> str:
        if _pid_of(line) != pid:
            return line
        parts = line.split("\t")
        parts += [""] * (3 - len(parts))
        parts[2] = identity
        return "\t".join(parts)

    _rewrite(path, update)


def host_pid(path: str) -> int | None:
    """The pid of the record's first host row."""
    for line in files.lines(files.read_text(path) or ""):
        role, pid, _ = files.tsv(line, 3)
        if role == "host":
            return pid_from(pid)
    return None


def read_pid_identity(path: str, *, terminated: bool = False) -> PidIdentity | None:
    """The record's first line, or None when it has none.

    With `terminated`, an unterminated first line counts as none, as a bash
    `read` that hits the end of the file fails.
    """
    text = files.read_text(path) or ""
    found = files.complete_lines(text) if terminated else files.lines(text)
    if not found:
        return None
    pid, identity = files.tsv(found[0], 2)
    return PidIdentity(pid_from(pid), identity)


def write_pid_identity(path: str, pid: int, identity: str) -> bool:
    """Write the record, then read it back: True only when the path is a regular
    file holding exactly this pid and identity."""
    expected = f"{pid}\t{identity}"
    return (
        files.replace(path, files.encode(expected + "\n"))
        and not os.path.islink(path)
        and os.path.isfile(path)
        and files.output(files.read_text(path) or "") == expected
    )


def log(path: str, text: str, now: int) -> None:
    try:
        with open(path, "ab") as handle:
            handle.write(files.encode(f"{now}\t{text}\n"))
    except OSError:
        # The ledger is a diagnostic record. A host that cannot write it keeps working.
        return
    content = files.read_text(path)
    if content is None or content.count("\n") <= LEDGER_LIMIT:
        return
    kept = files.lines(content)[-LEDGER_KEEP:]
    files.replace(path, files.encode("".join(line + "\n" for line in kept)))


@dataclass(frozen=True)
class Ledger:
    """The host's ledger of where every close went."""

    path: str
    stops: Stops

    def log(self, text: str) -> None:
        with self.stops.shielded():
            log(self.path, text, int(time.time()))


def _rewrite(path: str, keep_line: Callable[[str], str | None]) -> None:
    text = files.read_text(path)
    if text is None:
        return
    kept = [line for line in map(keep_line, files.lines(text)) if line is not None]
    files.replace(path, files.encode("".join(line + "\n" for line in kept)))


def _pid_of(line: str) -> int | None:
    parts = line.split("\t")
    return pid_from(parts[1]) if len(parts) > 1 else None
