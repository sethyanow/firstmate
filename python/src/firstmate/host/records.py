"""The host's process record and its ledger.

The process record (state/.supervision-host) holds one `role<TAB>pid<TAB>identity`
row per process the host runs, so a successor host stops exactly those.
The ledger (state/.supervision-host.log) holds one `epoch<TAB>text` row per event
and keeps its last 400 rows once it passes 600.
"""

import os

from .. import files

LEDGER_LIMIT = 600
LEDGER_KEEP = 400


def rows(path):
    """Every complete row of a record, split into role, pid, and identity."""
    return [files.tsv(line, 3) for line in files.complete_lines(files.read_text(path) or "")]


def append(path, role, pid, identity):
    try:
        with open(path, "ab") as handle:
            handle.write(files.encode(f"{role}\t{pid}\t{identity}\n"))
    except OSError:
        pass


def _rewrite(path, keep_line):
    text = files.read_text(path)
    if text is None:
        return
    kept = []
    for line in files.lines(text):
        kept_line = keep_line(line)
        if kept_line is not None:
            kept.append(kept_line)
    files.replace(path, files.encode("".join(line + "\n" for line in kept)))


def forget(path, pid):
    """Drop every row of `pid`."""
    if not os.path.isfile(path):
        return
    _rewrite(path, lambda line: line if _pid_of(line) != str(pid) else None)


def refresh(path, pid, identity):
    """Record `pid` under `identity` when a row holds another identity for it."""
    if not identity or not os.path.isfile(path):
        return
    found = [files.tsv(line, 3) for line in files.lines(files.read_text(path) or "")]
    if not any(row[1] == str(pid) and row[2] != identity for row in found):
        return

    def update(line):
        if _pid_of(line) != str(pid):
            return line
        parts = line.split("\t")
        parts += [""] * (3 - len(parts))
        parts[2] = identity
        return "\t".join(parts)

    _rewrite(path, update)


def host_pid(path):
    """The pid of the record's first host row, or empty."""
    for line in files.lines(files.read_text(path) or ""):
        role, pid, _ = files.tsv(line, 3)
        if role == "host":
            return pid
    return ""


def log(path, text, now):
    try:
        with open(path, "ab") as handle:
            handle.write(files.encode(f"{now}\t{text}\n"))
    except OSError:
        return
    content = files.read_text(path)
    if content is None or content.count("\n") <= LEDGER_LIMIT:
        return
    kept = files.lines(content)[-LEDGER_KEEP:]
    files.replace(path, files.encode("".join(line + "\n" for line in kept)))


def _pid_of(line):
    parts = line.split("\t")
    return parts[1] if len(parts) > 1 else ""
