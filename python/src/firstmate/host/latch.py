"""The broken-session latch (docs/supervision-host.md "The broken-session latch").

state/.supervision-host-health holds `key=`, `errors=`, `cooldown=`, and
`retry_after=` lines; the key ties them to one main session, engine, and model.
"""

from dataclasses import dataclass, replace

from .. import files
from .settings import numeric_or


@dataclass(frozen=True)
class Latch:
    errors: int = 0
    cooldown: int = 0
    retry: int = 0


def load(path, key):
    """The latch kept under `key`, or a clear one."""
    if key is None or files.field(path, "key") != key:
        return Latch()
    return Latch(
        errors=numeric_or(files.field(path, "errors"), 0),
        cooldown=numeric_or(files.field(path, "cooldown"), 0),
        retry=numeric_or(files.field(path, "retry_after"), 0),
    )


def save(path, key, latch):
    if key is None:
        return
    files.replace(path, files.encode(
        f"key={key}\nerrors={latch.errors}\ncooldown={latch.cooldown}\nretry_after={latch.retry}\n"))


def after_turn(latch, engine_error, reports, now, first_cooldown, cooldown_max):
    """Fold one finished turn into the latch.

    Returns the new latch, the one line main is owed when it trips for the
    first time (or empty), and the ledger rows the change records.
    """
    if engine_error:
        latch = replace(latch, errors=latch.errors + 1)
        if latch.errors < 2 and latch.cooldown == 0:
            return latch, "", []
        note = ""
        if latch.cooldown == 0:
            cooldown = first_cooldown
            note = (f"supervision-host: the supervision session is paused after repeated engine errors; every wake "
                    f"reaches you for the next {cooldown // 60} minutes, then one wake probes it again")
        else:
            cooldown = min(latch.cooldown * 2, cooldown_max)
        latch = replace(latch, cooldown=cooldown, retry=now + cooldown)
        return latch, note, [f"latch\terrors={latch.errors}\tcooldown={cooldown}s"]
    if reports > 0:
        logged = ["recovered\tafter a successful probe"] if latch.cooldown != 0 else []
        return Latch(), "", logged
    if latch.cooldown > 0 and latch.retry <= now:
        return replace(latch, retry=now + latch.cooldown), "", []
    return latch, "", []
