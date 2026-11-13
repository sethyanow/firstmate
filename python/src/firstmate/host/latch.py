"""The broken-session latch (docs/supervision-host.md "The broken-session latch").

state/.supervision-host-health holds `key=`, `errors=`, `cooldown=`, and
`retry_after=` lines. The key ties them to one main session, engine, and model.
"""

from dataclasses import dataclass, field, replace

from firstmate import files

from .settings import numeric_or

# The consecutive engine errors that trip the latch.
TRIP_ERRORS = 2
COOLDOWN_MAX = 3600


@dataclass(frozen=True)
class Latch:
    errors: int = 0
    cooldown: int = 0
    retry: int = 0


@dataclass(frozen=True)
class LatchChange:
    """A latch after one turn, the line main is owed when it first trips, and
    the ledger rows the change records."""

    latch: Latch
    note: str = ""
    logged: tuple[str, ...] = field(default_factory=tuple)


def load(path: str, key: str | None) -> Latch:
    """The latch kept under `key`, or a clear one."""
    text = files.read_text(path) or ""
    if key is None or files.field_of(text, "key") != key:
        return Latch()
    return Latch(
        errors=numeric_or(files.field_of(text, "errors"), 0),
        cooldown=numeric_or(files.field_of(text, "cooldown"), 0),
        retry=numeric_or(files.field_of(text, "retry_after"), 0),
    )


def save(path: str, key: str | None, latch: Latch) -> None:
    if key is None:
        return
    files.replace(
        path,
        files.encode(
            f"key={key}\nerrors={latch.errors}\ncooldown={latch.cooldown}\n"
            f"retry_after={latch.retry}\n"
        ),
    )


def cooling(paused_until: int | None, now: int) -> bool:
    """True while the latch holds main to every wake: before the probe time."""
    return paused_until is not None and now < paused_until


def after_turn(
    latch: Latch, *, engine_error: bool, reports: int, now: int, first_cooldown: int
) -> LatchChange:
    """Fold one finished turn into the latch."""
    if engine_error:
        latch = replace(latch, errors=latch.errors + 1)
        if latch.errors < TRIP_ERRORS and latch.cooldown == 0:
            return LatchChange(latch)
        note = ""
        if latch.cooldown == 0:
            cooldown = first_cooldown
            note = (
                "supervision-host: the supervision session is paused after repeated engine "
                f"errors; every wake reaches you for the next {cooldown // 60} minutes, then "
                "one wake probes it again"
            )
        else:
            cooldown = min(latch.cooldown * 2, COOLDOWN_MAX)
        latch = replace(latch, cooldown=cooldown, retry=now + cooldown)
        return LatchChange(latch, note, (f"latch\terrors={latch.errors}\tcooldown={cooldown}s",))
    if reports > 0:
        logged = ("recovered\tafter a successful probe",) if latch.cooldown != 0 else ()
        return LatchChange(Latch(), "", logged)
    if latch.cooldown > 0 and latch.retry <= now:
        return LatchChange(replace(latch, retry=now + latch.cooldown))
    return LatchChange(latch)
