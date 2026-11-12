"""The host's decisions, each a function of plain values the park gathered."""

import enum
from dataclasses import dataclass

from .scope import Scope


class Route(enum.Enum):
    """Where a close goes before any turn starts."""

    # The arm's own failure or attach result, which the owner judges as the arm's.
    NO_WAKE = enum.auto()
    # The away daemon owns triage while its flag exists.
    AWAY_DAEMON = enum.auto()
    ATTENDED = enum.auto()
    AWAY = enum.auto()


def route(reason: str, *, away_daemon: bool, away: bool) -> Route:
    if not reason:
        return Route.NO_WAKE
    if away_daemon:
        return Route.AWAY_DAEMON
    return Route.AWAY if away else Route.ATTENDED


@dataclass(frozen=True)
class Refusal:
    """Why an attended close stays with main. The offer rule's refusal is the
    main-only one, whose successor cycle is left running for main."""

    why: str
    main_only: bool = False


def attended_refusal(
    *, ready: bool, unready: str, main_known: bool, cooling: bool
) -> Refusal | None:
    """Why an attended close stays with main before its offer is judged, or None."""
    if not ready:
        return Refusal(unready)
    if not main_known:
        return Refusal("the main session could not be identified")
    if cooling:
        return Refusal("the supervision session is cooling down after engine errors")
    return None


def offer_refusal(offer: Scope | None) -> Refusal | None:
    """Why the branch's offer keeps an attended close with main, or None.
    No offer means its eligibility could not be computed."""
    if offer is None:
        return Refusal("branch eligibility could not be computed")
    if not offer.eligible:
        return Refusal("main-only", main_only=True)
    return None


class AwayRefusal(enum.Enum):
    """Why an away close cannot reach the engine."""

    HOST_OFF = enum.auto()
    NO_ENGINE = enum.auto()
    NO_NODE = enum.auto()
    COOLING = enum.auto()


def away_refusal(
    *, runs_host: bool, engine: str, node_present: bool, cooling: bool
) -> AwayRefusal | None:
    if not runs_host:
        return AwayRefusal.HOST_OFF
    if not engine:
        return AwayRefusal.NO_ENGINE
    if not node_present:
        return AwayRefusal.NO_NODE
    if cooling:
        return AwayRefusal.COOLING
    return None


def away_refusal_line(refusal: AwayRefusal, *, problem: str, until: str) -> str:
    """The reason main gets an away close: `problem` names the missing engine,
    `until` the end of the latch's cooldown."""
    if refusal is AwayRefusal.HOST_OFF:
        return "the home no longer runs the supervision host"
    if refusal is AwayRefusal.NO_ENGINE:
        return f"no supervision engine runs here: {problem}; this wake is yours"
    if refusal is AwayRefusal.NO_NODE:
        return "node is required to compute branch eligibility; this wake is yours"
    return (
        f"the away session is paused after repeated engine errors until {until}; "
        "this wake is yours"
    )


# The status of a command that could not run.
_CANNOT_RUN = 127


@dataclass(frozen=True)
class TurnEnd:
    """What a finished engine turn left: its exit status, whether that status is
    its bound's, its usage line, the reports it recorded, and the granted rows
    still queued."""

    rc: int
    timed_out: bool
    usage: str
    receipts: int
    unacked: tuple[str, ...]

    @property
    def complete_result(self) -> bool:
        """The engine's usage line reports a complete result without an error."""
        return self.usage.startswith("error=0")

    @property
    def engine_error(self) -> bool:
        return self.rc != 0 or not self.complete_result

    @property
    def handled(self) -> bool:
        """A turn handled its wake when it exited cleanly, recorded a report, and
        left none of its granted rows queued."""
        return not self.engine_error and self.receipts > 0 and not self.unacked


def turn_failure(end: TurnEnd, *, turn_timeout: int, engine: str) -> str:
    """Why a turn that did not handle its wake left it for main."""
    if end.timed_out:
        return f"the engine turn hit its {turn_timeout}s bound"
    if end.rc == _CANNOT_RUN:
        return f"the {engine} engine could not run"
    if end.rc != 0:
        return f"the engine turn failed (exit {end.rc})"
    if not end.complete_result:
        return "the engine turn ended with an error or an incomplete result"
    if end.receipts == 0:
        return "the engine turn recorded no outcome for its wake"
    return f"the engine turn left its granted wake rows {' '.join(end.unacked)} unacknowledged"


class ReturnedKind(enum.Enum):
    """What the captain's return during an away turn left for main."""

    NONE = enum.auto()
    OUTCOMES = enum.auto()
    LOOKUP_FAILED = enum.auto()


@dataclass(frozen=True)
class Returned:
    """The visible outcomes an away turn recorded after the captain returned."""

    kind: ReturnedKind
    receipt_seqs: str = ""
    seqs: str = ""
    lines: str = ""


@dataclass(frozen=True)
class Handback:
    """A close main receives: why, further "supervision-host:" lines, whether the
    close itself is printed ahead of them, and whether the notice carries the
    local time."""

    why: str
    further: str = ""
    with_close: bool = True
    stamped: bool = False


def _joined(*lines: str) -> str:
    return "\n".join(line for line in lines if line)


def returned_handback(returned: Returned, *, failure: str | None, health_note: str) -> Handback:
    """The hand-back of a turn the captain returned during: its return brief was
    rendered before the turn's visible outcomes existed, so main relays them,
    whether the turn handled its wake (`failure` None) or not."""
    if failure is None:
        opening = "the captain returned while the away session was handling this wake"
        relay = (
            ", which it finished after the return brief was rendered; relay its visible outcomes"
        )
        unverified = ", but the recorded outcomes could not be verified; main must review them"
    else:
        opening = (
            f"the away session could not take this wake: {failure}; this wake is yours, "
            "and the captain returned during its turn"
        )
        relay = ", so relay the visible outcomes it recorded"
        unverified = ", but the recorded outcomes could not be verified"
    if returned.kind is ReturnedKind.LOOKUP_FAILED:
        warning = (
            "supervision-host: outcome lookup failed for turn receipt rows "
            f"{returned.receipt_seqs or 'unknown'}; visible outcomes may require manual review"
        )
        return Handback(opening + unverified, _joined(warning, health_note))
    return Handback(
        f"{opening}{relay} (store rows {returned.seqs}, listed next and in "
        "bin/fm-branch-outcome.sh list) to the captain",
        _joined(returned.lines, health_note),
    )


def failed_handback(failure: str, *, away: bool, health_note: str) -> Handback:
    session = "away session" if away else "supervision session"
    return Handback(
        f"the {session} could not take this wake: {failure}; this wake is yours", health_note
    )


def captain_handback(captain_seqs: str, health_note: str) -> Handback:
    """An attended turn's captain outcomes are main's to process: the close itself
    was handled, so only the host's lines reach main."""
    return Handback(
        "branch-outcome: the supervision session handled this wake and recorded captain "
        f"outcomes for you (store rows {captain_seqs}); run bin/fm-wake-drain.sh, act on its "
        "BRANCH OUTCOMES section, and acknowledge them as it prints",
        health_note,
        with_close=False,
        stamped=True,
    )


def after_turn(
    *,
    failure: str | None,
    away: bool,
    returned: Returned,
    captain_seqs: str,
    health_note: str,
) -> Handback | None:
    """Where a close goes after its engine turn. None when the host parks on.

    `failure` is why the turn did not handle its wake, or None when it did.
    `away` is the turn's posture, and `captain_seqs` the captain outcomes it
    recorded when the captain is present after it.
    """
    if returned.kind is not ReturnedKind.NONE:
        return returned_handback(returned, failure=failure, health_note=health_note)
    if failure is not None:
        return failed_handback(failure, away=away, health_note=health_note)
    if captain_seqs:
        return captain_handback(captain_seqs, health_note)
    return None
