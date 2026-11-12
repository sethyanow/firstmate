"""The current turn's report receipts (state/.supervision-host-receipts).

bin/fm-branch-report.sh appends one `turn<TAB>seq<TAB>verdict<TAB>task` row per
report an engine turn records; the host empties the file when a turn starts.
"""

from dataclasses import dataclass

from firstmate import files


@dataclass(frozen=True)
class Receipt:
    turn: str
    seq: str
    verdict: str
    task: str


def read(path: str) -> list[Receipt] | None:
    """Every row, or None when the file cannot be read."""
    text = files.read_text(path)
    if text is None:
        return None
    return [Receipt(*files.tsv(line, 4)) for line in files.lines(text)]


def of_turn(path: str, turn: str) -> list[Receipt] | None:
    """The rows one turn recorded, or None when the file cannot be read."""
    found = read(path)
    return None if found is None else [receipt for receipt in found if receipt.turn == turn]


def clear(path: str) -> None:
    files.write(path, "")
