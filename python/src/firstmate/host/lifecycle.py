"""Taking the host record from a predecessor, and the cleanup every exit runs."""

import os
from dataclasses import dataclass

from firstmate import files

from . import records
from .context import HostContext
from .procs import Held, ProcessControl

# Room for a predecessor host's own cleanup, beyond the engine grace.
_PREDECESSOR_EXTRA_SECONDS = 20
_ENGINE_EXTRA_SECONDS = 5
_ARM_SECONDS = 10
_TURN_FILE_KINDS = ("arm", "warm", "descendants", "result", "errors", "readback")


@dataclass(frozen=True)
class Activation:
    """An activated host: the successor arm a predecessor's pass-through left for
    main while it still answers to its recorded identity, which the first cycle
    takes over."""

    left_arm: int | None


def activate(ctx: HostContext, procs: ProcessControl) -> Activation | None:
    """Stop whatever a predecessor host left running, then take the record;
    None when the host cannot activate.
    The auto-arm admits one generation at a time, so a predecessor still alive
    here was superseded (its owner died or went stale) or crashed mid-cleanup.
    """
    paths, grace = ctx.paths, ctx.settings.engine_grace
    try:
        os.makedirs(paths.state, exist_ok=True)
    except OSError:
        return None
    if not _stop_predecessor(ctx, procs):
        return None
    if os.path.isfile(paths.engine_pid_file):
        engine = records.read_pid_identity(paths.engine_pid_file)
        if engine is not None:
            procs.stop_recorded(engine.pid, engine.identity, grace + _ENGINE_EXTRA_SECONDS)
        files.remove(paths.engine_pid_file)
    for row in records.rows(paths.host_record):
        if row.role == "arm":
            procs.stop_recorded(row.pid, row.identity, _ARM_SECONDS)
    _drop_turn_files(ctx)
    left = _left_arm(ctx, procs)
    with ctx.stops.shielded():
        written = records.write_host(
            paths.host_record, ctx.facts.pid, ctx.libs.identity_of(ctx.facts.pid)
        )
    if not written:
        return None
    ctx.release_branch_leases()
    return Activation(left)


def _stop_predecessor(ctx: HostContext, procs: ProcessControl) -> bool:
    """Stop the predecessor host, with room for its own cleanup (which stops its
    engine and arms), and the acknowledged hand-back owner; False while that
    owner cannot be read or still runs."""
    paths = ctx.paths
    for row in records.rows(paths.host_record):
        if row.role == "host" and row.pid != ctx.facts.pid:
            procs.stop_recorded(
                row.pid, row.identity, ctx.settings.engine_grace + _PREDECESSOR_EXTRA_SECONDS
            )
    if not os.path.isfile(paths.handoff_owner):
        return True
    owner = records.read_pid_identity(paths.handoff_owner, terminated=True)
    if owner is None:
        return False
    procs.stop_recorded(owner.pid, owner.identity, _ARM_SECONDS)
    return not procs.answers_to(owner.pid, owner.identity)


def _drop_turn_files(ctx: HostContext) -> None:
    """A predecessor killed outright ran no cleanup: reap the engine
    descendants its turn recorded, then drop that turn's files."""
    paths = ctx.paths
    for name in sorted(os.listdir(paths.state)):
        if (
            name.startswith(".supervision-host-descendants.")
            and not name.endswith((".pids", ".next"))
            and os.path.isfile(paths.at(name))
        ):
            ctx.libs.reap_descendants(paths.at(name))
    prefixes = tuple(f".supervision-host-{kind}." for kind in _TURN_FILE_KINDS)
    for name in os.listdir(paths.state):
        if name.startswith(prefixes):
            files.remove(paths.at(name))
    files.remove(paths.turn_file, paths.mirror_feed)


def _left_arm(ctx: HostContext, procs: ProcessControl) -> int | None:
    """The successor a pass-through left for main, while it still answers to its
    recorded identity; its record goes only once it does not."""
    path = ctx.paths.left_record
    if not os.path.isfile(path):
        return None
    left = records.read_pid_identity(path)
    if left is not None and procs.answers_to(left.pid, left.identity):
        return left.pid
    files.remove(path)
    return None


def cleanup(ctx: HostContext, procs: ProcessControl, held: Held) -> None:
    """Stop what the host holds and release what it claimed.

    The branch leases and the turn records belong to the host whose row the
    record holds: none to a host stopped before it activated, and a
    successor's once that successor activated.
    """
    ctx.stops.disarm()
    if held.engine is not None:
        procs.stop_engine(held.engine)
    procs.retire(held.successor)
    procs.retire(held.arm)
    if held.grant:
        ctx.release_grant()
        ctx.grant("deactivate", str(ctx.facts.pid), ctx.facts.gen)
    own = ctx.paths.at(".supervision-host-")
    files.remove(*(path for path in held.turn_files if path.startswith(own)))
    if records.host_pid(ctx.paths.host_record) == ctx.facts.pid:
        ctx.release_branch_leases()
        files.remove(ctx.paths.turn_file, ctx.paths.engine_pid_file, ctx.paths.host_record)
