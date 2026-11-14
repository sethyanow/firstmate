"""Watcher cycles: the arm the host parks on and the successor it starts for a turn."""

import enum
import os
import time
from dataclasses import dataclass

from firstmate import files
from firstmate.processes import NULL, TO_STDOUT, Streams

from . import records, text
from .context import HostContext
from .procs import Child, Held, ProcessControl

_VERIFY_POLL_SECONDS = 0.2


class Slot(enum.Enum):
    ARM = enum.auto()
    SUCCESSOR = enum.auto()


class Left(enum.Enum):
    """How leaving a successor cycle running for main went."""

    LEFT = enum.auto()
    SUCCESSOR_FAILED = enum.auto()
    # An actionable banner cannot commit while the marker reads handling.
    DOWNTIME_UNRESTORED = enum.auto()


@dataclass(frozen=True)
class Cycles:
    ctx: HostContext
    procs: ProcessControl
    held: Held

    def start(
        self, slot: Slot, predecessor: int | None, *args: str, own_group: bool = False
    ) -> Child | None:
        """Start one watcher cycle in `slot`. The started Child, or None. The slot
        holds the child before a stop signal can end the start, so the cleanup
        stops it."""
        ctx = self.ctx
        env = {**ctx.env, "FM_GUARD_GRACE": ctx.facts.grace}
        if predecessor is not None:
            env["FM_WATCH_PREDECESSOR_ARM_PID"] = str(predecessor)
        with ctx.stops.shielded():
            try:
                fd, out = files.mkstemp(ctx.paths.at(".supervision-host-arm"))
            except OSError:
                return None
            try:
                # An arm left for main outlives this host, and Claude tears the
                # hook's process group down after the exit-2 rewake, so it gets
                # a group of its own (the shape start_handling_successor in
                # bin/fm-claude-stop-autoarm.sh uses).
                started = ctx.libs.bash.spawn(
                    ctx.script("fm-watch-arm.sh", *args),
                    Streams(NULL, fd, TO_STDOUT),
                    env=env,
                    own_group=own_group,
                )
            except OSError:
                files.remove(out)
                return None
            finally:
                os.close(fd)
            child = Child(started, out)
            if slot is Slot.ARM:
                self.held.arm = child
            else:
                self.held.successor = child
            self.procs.record("arm", child.pid)
        return child

    def start_successor(self, predecessor: int | None) -> text.Verified | None:
        """Start the successor cycle and wait until it proves a live watcher."""
        child = self.start(Slot.SUCCESSOR, predecessor, own_group=True)
        if child is None:
            return None
        deadline = time.monotonic() + self.ctx.settings.ready_timeout
        while True:
            verified = text.verified_watcher(files.read_text(child.out) or "")
            if verified:
                self.procs.refresh(child.pid)
                return verified
            if not child.alive() or time.monotonic() >= deadline:
                return None
            time.sleep(_VERIFY_POLL_SECONDS)

    def detach_successor(self, first: str) -> bool:
        """Record the successor for the next host to take over, then drop it from
        this host's cleanup without stopping it. A successor whose record does not
        read back stays held, so the cleanup stops it and main's next turn end
        arms a fresh cycle. Its capture file goes now: the arm keeps writing to
        the descriptor it already holds."""
        successor = self.held.successor
        if successor is None:
            return True
        identity = self.ctx.libs.identity_of(successor.pid)
        with self.ctx.stops.shielded():
            recorded = bool(identity) and records.write_pid_identity(
                self.ctx.paths.left_record, successor.pid, identity
            )
        if not recorded:
            self.ctx.ledger.log(f"pass-through\tsuccessor-unrecorded\t{first}")
            return False
        self.procs.forget(successor.pid)
        files.remove(successor.out)
        self.held.successor = None
        return True

    def leave_for_main(self, predecessor: int | None, first: str) -> Left:
        """Start a successor and leave it running for main, which receives this close
        only on downtime, even when a preceding engine turn left handling behind
        (autoarm_commit in bin/fm-claude-stop-autoarm.sh)."""
        verified = self.start_successor(predecessor)
        if verified is None:
            self.ctx.ledger.log(f"pass-through\tsuccessor-unverified\t{first}")
            return Left.SUCCESSOR_FAILED
        if verified.generation and not self.ctx.libs.publish_downtime():
            self.ctx.ledger.log(f"pass-through\tdowntime-unrestored\t{first}")
            return Left.DOWNTIME_UNRESTORED
        return Left.LEFT if self.detach_successor(first) else Left.SUCCESSOR_FAILED

    def retire_successor(self) -> None:
        """Stop the successor cycle: the state main's own turn end starts from
        without the host."""
        if self.held.successor is None:
            return
        self.procs.retire(self.held.successor)
        self.held.successor = None
        self.ctx.stop_watcher()

    def retire_all(self) -> None:
        """Stop the current and successor arms."""
        self.procs.retire(self.held.arm)
        self.procs.retire(self.held.successor)
        self.held.arm = None
        self.held.successor = None
