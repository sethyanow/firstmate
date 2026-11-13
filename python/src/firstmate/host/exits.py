"""Every way the host ends its park: what it prints, and in what order.

Each exit prints the close read so far, then its own lines, in one write, so
an owner reading a stream sees the whole exit at once.
"""

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn

from firstmate import files
from firstmate.signals import Exit

from . import text
from .context import HostContext
from .cycles import Cycles
from .decide import Handback
from .procs import Held, ProcessControl
from .text import Close

BOUNDARY_HANDBACK = (
    "supervision-host: cycle boundary - the host ended its park at its bound; "
    "drain, acknowledge, and end the turn"
)
BOUNDARY_LINE = BOUNDARY_HANDBACK + ", and the next park starts on its own"
NO_LONGER_OWNER = "this session no longer owns supervision"
_HANDOFF_CHECK = (
    "check: supervision-handoff - the host handed supervision to main; drain, handle, "
    "and acknowledge this hand-back"
)
_OWNER_POLL_SECONDS = 0.2


@dataclass(frozen=True)
class Exits:
    ctx: HostContext
    procs: ProcessControl
    held: Held
    cycles: Cycles

    def emit(self, close: str, *lines: str) -> None:
        composed = text.compose(close, lines)
        if composed:
            self.ctx.out.write(composed + "\n")

    def pass_through(self, close: Close) -> NoReturn:
        """Print the close exactly as the arm printed it."""
        self.emit(close.text)
        raise Exit(0)

    def stand_down(self, close: Close, why: str) -> NoReturn:
        self.ctx.ledger.log(f"stand-down\t{why}")
        self.emit(close.text, f"supervision-host stood down: {why}")
        raise Exit(0)

    def exit_to_main(self, close: Close, handback: Handback, *, generation: str) -> NoReturn:
        """Hand the close to main: stop the successor cycle, print the close, why,
        and any further "supervision-host:" lines, and exit. `generation` is the
        recovery generation the successor started, whose downtime goes back."""
        notice = f"supervision-host: {handback.why}"
        if handback.stamped:
            notice = f"{notice} {self.ctx.libs.local_time()}"
        if not handback.with_close:
            close = Close("", close.arm_pid)
        if self.handback_acknowledged():
            self.acknowledged_handback(close, (notice, handback.further))
        self.cycles.retire_successor()
        if generation and not self.ctx.libs.publish_downtime():
            self.ctx.ledger.log(f"to-main\tdowntime-unrestored\t{handback.why}")
            # Close readers suppress acknowledged wakes, so failure must stay non-actionable.
            print(
                "supervision-host hand-back failed: watcher downtime could not be restored; "
                f"the dropped close was: {handback.why}",
                file=sys.stderr,
            )
            raise Exit(1)
        self.ctx.ledger.log(f"to-main\t{handback.why}")
        self.emit(close.text, notice, handback.further)
        raise Exit(0)

    def boundary_exit(self, close: Close) -> NoReturn:
        """End the park at the boundary: stop the current and successor arms and
        this home's watcher, print any close already read so main drains it,
        then the boundary line."""
        self.ctx.ledger.log(f"boundary\tafter {self.ctx.clock.elapsed()}s")
        if self.handback_acknowledged():
            self.acknowledged_handback(close, (BOUNDARY_HANDBACK,))
        self.cycles.retire_all()
        self.ctx.stop_watcher()
        self.emit(close.text, BOUNDARY_LINE)
        raise Exit(0)

    # --- acknowledged hand-back --------------------------------------------------
    # Claude only, with config/supervision-handoff-ack: every hand-back first
    # queues a token-bound check and verifies a detached
    # bin/fm-supervision-handoff.sh owner, which keeps a watcher until main
    # acknowledges the check.

    def handback_acknowledged(self) -> bool:
        return self.ctx.facts.primary == "claude" and self.ctx.paths.configured(
            "supervision-handoff-ack"
        )

    def acknowledged_handback(self, close: Close, lines: Sequence[str]) -> NoReturn:
        """Hand back through a verified detached owner, then print the close and
        `lines`. On any failure, print why and exit 1."""
        ctx, held = self.ctx, self.held
        if not ctx.still_owner():
            self.stand_down(close, NO_LONGER_OWNER)
        predecessor = next(
            pid
            for pid in (
                held.successor.pid if held.successor else None,
                held.arm.pid if held.arm else None,
                close.arm_pid,
                ctx.facts.pid,
            )
            if pid
        )
        self.procs.retire(held.arm)
        held.arm = None
        self.cycles.retire_successor()
        if not self._queue_handoff_check():
            self.emit(
                close.text,
                *lines,
                "supervision-host: the acknowledged hand-back could not be recorded",
            )
            raise Exit(1)
        if self._start_handoff_owner(predecessor):
            ctx.ledger.log(f"handoff\t{ctx.facts.gen}")
            self.emit(close.text, *lines)
            raise Exit(0)
        self.emit(
            close.text,
            *lines,
            "supervision-host: the acknowledged hand-back successor could not be verified",
        )
        raise Exit(1)

    def _queue_handoff_check(self) -> bool:
        """Record this hand-back's token, queue its check, and publish downtime."""
        ctx = self.ctx
        with ctx.stops.shielded():
            if not files.replace(ctx.paths.handoff_pending, files.encode(ctx.facts.gen + "\n")):
                return False
        appended = ctx.libs.append_wake(
            "check", f"supervision-handoff:{ctx.facts.gen}", _HANDOFF_CHECK
        )
        return appended and ctx.libs.publish_downtime(quiet=False)

    def _start_handoff_owner(self, predecessor: int) -> bool:
        """Start the detached hand-back owner. True once it proves a healthy watcher."""
        ctx = self.ctx
        try:
            fd, out = files.mkstemp(ctx.paths.at(".supervision-handoff-start"))
        except OSError:
            return False
        with ctx.stops.shielded():
            try:
                proc = subprocess.Popen(
                    [
                        "nohup",
                        *ctx.script("fm-supervision-handoff.sh", ctx.facts.gen, str(predecessor)),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=fd,
                    stderr=subprocess.STDOUT,
                    process_group=0,
                    env={
                        **ctx.env,
                        "FM_SUPERVISION_HOST_READY_TIMEOUT": str(ctx.settings.ready_timeout),
                    },
                )
            except OSError:
                proc = None
            os.close(fd)
            if proc is not None:
                self.procs.record("handoff", proc.pid)
        deadline = time.monotonic() + ctx.settings.ready_timeout
        while proc is not None and proc.poll() is None and time.monotonic() < deadline:
            if text.verified_watcher(files.read_text(out) or "") and ctx.libs.watcher_healthy(
                ctx.facts.grace
            ):
                self.procs.forget(proc.pid)
                files.remove(out)
                return True
            time.sleep(_OWNER_POLL_SECONDS)
        if proc is not None:
            proc.terminate()
        files.remove(out)
        return False
