"""The park boundary (the firstmate.host docstring, THE PARK BOUNDARY)."""

import enum
from collections.abc import Mapping
from dataclasses import dataclass

from .context import ParkClock
from .settings import Settings


class Boundary(enum.Enum):
    INSIDE = enum.auto()
    REACHED = enum.auto()
    # The verified Codex asynchronous owner has no short hook deadline: its
    # quiet checkpoint keeps the same watcher alive, so the park's clock starts
    # over instead of ending the park.
    QUIET_RESTART = enum.auto()


def model_free(primary: str, env: Mapping[str, str]) -> bool:
    return primary == "codex" and env.get("FM_CODEX_MODEL_FREE_PARK", "0") == "1"


def check(elapsed: int, *, park_seconds: int, quiet: bool) -> Boundary:
    if elapsed < park_seconds:
        return Boundary.INSIDE
    return Boundary.QUIET_RESTART if quiet else Boundary.REACHED


def turn_crosses(elapsed: int, settings: Settings, *, quiet: bool) -> bool:
    """True when an engine turn started now could still be running at the turn
    limit (the boundary unless the owner set a later one)."""
    if quiet:
        return False
    return elapsed + settings.turn_timeout + settings.engine_grace >= settings.park_limit


@dataclass(frozen=True)
class ParkBoundary:
    """The boundary of one park, read from its clock."""

    clock: ParkClock
    settings: Settings
    quiet: bool

    def check(self) -> Boundary:
        return check(
            self.clock.elapsed(), park_seconds=self.settings.park_seconds, quiet=self.quiet
        )

    def turn_crosses(self) -> bool:
        return turn_crosses(self.clock.elapsed(), self.settings, quiet=self.quiet)
