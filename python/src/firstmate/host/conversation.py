"""The engine conversation and its record (state/.supervision-host-engine).

The record holds `engine=`, `model=`, `session=`, `key=` (the main session's),
`turns=`, `conversation_cost=`, `ended=` (the epoch of the last handled turn),
and `compacted=` (the `ended` value warm compaction last compacted) lines.
"""

import enum
from dataclasses import dataclass

from firstmate import files

from .libs import Engine
from .settings import numeric_or

# A rendered branch prompt shorter than this is a failed render.
PROMPT_MIN_BYTES = 1024


@dataclass(frozen=True)
class EngineRecord:
    engine: str = ""
    model: str = ""
    session: str = ""
    key: str = ""
    turns: int = 0
    # The running cost as the engine library reads and writes it: decimal text.
    cost: str = "0"
    ended: int = 0
    compacted: int = 0


class Mode(enum.Enum):
    NEW = "new"
    RESUME = "resume"


@dataclass(frozen=True)
class Conversation:
    """The engine conversation one turn runs in."""

    session: str
    mode: Mode
    turns: int
    cost: str
    key: str


def read(path: str) -> EngineRecord:
    text = files.read_text(path) or ""
    return EngineRecord(
        engine=files.field_of(text, "engine"),
        model=files.field_of(text, "model"),
        session=files.field_of(text, "session"),
        key=files.field_of(text, "key"),
        turns=numeric_or(files.field_of(text, "turns"), 0),
        cost=files.field_of(text, "conversation_cost") or "0",
        ended=numeric_or(files.field_of(text, "ended"), 0),
        compacted=numeric_or(files.field_of(text, "compacted"), 0),
    )


def write(path: str, record: EngineRecord) -> bool:
    """Write the record of a handled turn. `compacted` starts over."""
    return files.replace(
        path,
        files.encode(
            f"engine={record.engine}\nmodel={record.model}\nsession={record.session}\n"
            f"key={record.key}\nturns={record.turns}\nconversation_cost={record.cost}\n"
            f"ended={record.ended}\n"
        ),
    )


def mark_compacted(path: str, ended: int) -> None:
    """Record that warm compaction compacted the conversation idle since `ended`."""
    kept = [
        line
        for line in files.lines(files.read_text(path) or "")
        if not line.startswith("compacted=")
    ]
    files.replace(
        path, files.encode("".join(line + "\n" for line in kept) + f"compacted={ended}\n")
    )


def resumable(
    record: EngineRecord, *, key: str | None, engine: Engine, rotate_turns: int, prompt_ready: bool
) -> bool:
    """True when the recorded conversation is one the next turn resumes: it
    belongs to this main session, engine, and model, has turns left, and its
    prompt is rendered."""
    return bool(
        key
        and record.session
        and record.key == key
        and record.engine == engine.name
        and record.model == engine.model
        and record.turns < rotate_turns
        and prompt_ready
    )


def resumed(record: EngineRecord) -> Conversation:
    return Conversation(record.session, Mode.RESUME, record.turns, record.cost, record.key)
