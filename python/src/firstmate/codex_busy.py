"""Bind a Codex spawn to its new rollout and read its turn lifecycle.

Binding and verdict contract: bin/fm-busy-lib.sh.
"""

import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path


def sessions(home: Path, workspace: str) -> list[tuple[str, str, int]]:
    database = home / "state_5.sqlite"
    if not database.exists():
        return []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.1) as db:
        return db.execute(
            "SELECT id, rollout_path, created_at_ms FROM threads WHERE cwd = ? AND source = 'cli'",
            (workspace,),
        ).fetchall()


def bind(home: Path, workspace: str) -> str:
    workspace = str(Path(workspace).resolve())
    return json.dumps(
        {
            "home": str(home),
            "workspace": workspace,
            "prior": [row[0] for row in sessions(home, workspace)],
            "since_ms": time.time_ns() // 1_000_000,
        }
    )


def turn_state(log: Path, session: str, workspace: str) -> str:
    with log.open() as stream:
        header = json.loads(next(stream))
        meta = header["payload"]
        if header["type"] != "session_meta" or any(
            meta.get(key) != value
            for key, value in {
                "id": session,
                "cwd": workspace,
                "source": "cli",
                "originator": "codex-tui",
            }.items()
        ):
            return "unknown"
        active: set[str] = set()
        observed = False
        for line in stream:
            record = json.loads(line)
            if record.get("type") != "event_msg":
                continue
            event = record["payload"]
            kind = event.get("type")
            if kind not in {"task_started", "task_complete", "turn_aborted"}:
                continue
            turn = event.get("turn_id")
            if not isinstance(turn, str) or not turn:
                return "unknown"
            if kind == "task_started":
                active.add(turn)
            elif turn in active:
                active.remove(turn)
            else:
                return "unknown"
            observed = True
    return "busy" if active else "idle" if observed else "unknown"


def classify(binding: Path) -> str:
    data = json.loads(binding.read_text())
    home, workspace = Path(data["home"]), data["workspace"]
    candidates = [
        (session, path)
        for session, path, created in sessions(home, workspace)
        if session not in data["prior"] and created >= data["since_ms"]
    ]
    if len(candidates) != 1:
        return "unknown"
    session, path = candidates[0]
    return turn_state(Path(path), session, workspace)


def main(args: Sequence[str]) -> int:
    try:
        match args:
            case ["bind", home, workspace]:
                result = bind(Path(home).resolve(), workspace)
            case ["classify", binding]:
                result = classify(Path(binding))
            case _:
                return 2
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        StopIteration,
        sqlite3.Error,
    ):
        return 1
    sys.stdout.write(result + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
