"""The supervision host's park loop. The firstmate.host docstring owns its contract."""

import contextlib
import os
import shutil
import subprocess
import time
from typing import NoReturn

from firstmate import files
from firstmate.processes import CAPTURE
from firstmate.signals import Exit

from . import decide, latch, receipts, text
from .boundary import Boundary, ParkBoundary
from .context import HostContext
from .cycles import Cycles, Left, Slot
from .decide import Handback, Returned, ReturnedKind
from .exits import NO_LONGER_OWNER, Exits
from .libs import Engine
from .lifecycle import activate
from .procs import Held, ProcessControl
from .text import NO_CLOSE, Close
from .turn import TurnOutcome, TurnResult, Turns, judge_attended
from .warm import Warm

_MAIN_ONLY_DOWNTIME_FAILED = (
    "watcher: FAILED - the main-only hand-back could not restore watcher downtime\n"
)


class Park:
    def __init__(
        self,
        ctx: HostContext,
        procs: ProcessControl,
        held: Held,
        boundary: ParkBoundary,
    ) -> None:
        self.ctx = ctx
        self.procs = procs
        self.held = held
        self.boundary = boundary
        self.cycles = Cycles(ctx, procs, held)
        self.exits = Exits(ctx, procs, held, self.cycles)
        self.warm = Warm(ctx, procs, held, boundary)
        self.turns = Turns(ctx, held, boundary)
        # The first cycle's status line, printed as soon as the arm prints it
        # and left out of that cycle's close. Only the first close looks for it.
        self.first_close = True

    def stand_down_unless_owner(self) -> None:
        """Ownership first: a host that does not own supervision leaves the
        owner's host, processes, arms, and leases alone."""
        if not self.ctx.still_owner():
            self.exits.stand_down(NO_CLOSE, "this session does not own supervision")

    def run(self) -> NoReturn:
        ctx, facts = self.ctx, self.ctx.facts
        activation = activate(ctx, self.procs)
        if activation is None:
            ctx.out.write("supervision-host stood down: the host record could not be written\n")
            raise Exit(0)
        ctx.ledger.log(f"start\tgen={facts.gen}\tprimary={facts.primary}")
        if facts.restart:
            arm = self.cycles.start(Slot.ARM, facts.owner_predecessor, "--restart")
        elif activation.left_arm is not None:
            ctx.ledger.log(f"take-over\tarm={activation.left_arm}")
            arm = self.cycles.start(
                Slot.ARM, facts.owner_predecessor, "--take-over", str(activation.left_arm)
            )
        else:
            arm = self.cycles.start(Slot.ARM, facts.owner_predecessor)
        if arm is None:
            ctx.out.write(
                "watcher: FAILED - the supervision host could not start a watcher cycle\n"
            )
            raise Exit(1)
        while True:
            self.one_close()

    # --- parked -------------------------------------------------------------------

    def boundary_reached(self) -> bool:
        check = self.boundary.check()
        if check is Boundary.QUIET_RESTART:
            self.ctx.ledger.log(
                f"quiet-boundary\tafter {self.ctx.clock.elapsed()}s; watcher retained"
            )
            self.ctx.clock.restart()
            self.warm.restart()
        return check is Boundary.REACHED

    def stream_ready_line(self, arm_out: str) -> str:
        """Print the first cycle's status line once the arm has written it in
        full. The line printed, or empty while the arm has not written it."""
        line = text.status_line(files.read_text(arm_out) or "")
        if line:
            self.ctx.out.write(line + "\n")
        return line

    def await_close(self) -> Close | None:
        """Wait for the current arm to close. None when the park boundary arrives first."""
        arm = self.held.arm
        if arm is None:
            raise RuntimeError("the host parks only on a running arm")
        ready_line = ""
        while arm.alive():
            self.procs.refresh(arm.pid)
            if self.first_close and not ready_line:
                ready_line = self.stream_ready_line(arm.out)
            if self.boundary_reached():
                return None
            self.warm.tick(lambda: not arm.alive() or self.boundary_reached())
            with contextlib.suppress(subprocess.TimeoutExpired):
                arm.proc.wait(timeout=self.ctx.settings.poll)
        closed = files.output(files.read_text(arm.out) or "")
        if ready_line:
            # Already printed: drop its first occurrence from this first close.
            closed = text.without_status(closed, ready_line)
        self.first_close = False
        self.procs.forget(arm.pid)
        files.remove(arm.out)
        self.held.arm = None
        return Close(closed, arm.pid)

    # --- one close ------------------------------------------------------------------

    def one_close(self) -> None:
        if self.boundary_reached():
            self.exits.boundary_exit(NO_CLOSE)
        close = self.await_close()
        if close is None:
            self.exits.boundary_exit(NO_CLOSE)
        engine = self._engine_for(close)
        # A turn that could outlive the boundary would outlive the hook registration.
        if self.boundary.turn_crosses():
            self.exits.boundary_exit(close)
        generation = self._hand_off_to_successor(close)
        result = self.turns.handle(close.reason, engine)
        if result.outcome is TurnOutcome.KEPT_FOR_MAIN:
            self._keep_for_main(close, result.why, generation)
        if result.outcome is TurnOutcome.BOUNDARY:
            self.exits.boundary_exit(close)
        handback = self._after_turn(result)
        if handback is not None:
            self.exits.exit_to_main(close, handback, generation=generation)
        self._park_on_successor(result.turn)

    def _engine_for(self, close: Close) -> Engine:
        """The engine a close goes to. A close the owner judges as the arm's exits
        0, since a status above 128 tells the owner the host itself died."""
        ctx = self.ctx
        where = decide.route(
            close.reason,
            away_daemon=os.path.exists(ctx.paths.away_daemon_flag),
            away=ctx.libs.away_present(),
        )
        if where is decide.Route.NO_WAKE:
            ctx.ledger.log("pass-through\ta close without a wake")
            self.exits.pass_through(close)
        if where is decide.Route.AWAY_DAEMON:
            ctx.ledger.log(f"pass-through\tthe away daemon's flag exists\t{close.first_wake}")
            self.exits.pass_through(close)
        if where is decide.Route.ATTENDED:
            return self._attended_engine(close)
        return self._away_engine(close)

    def _park_on_successor(self, turn: str) -> None:
        """A handled close: settle the handling handoff and park on the successor."""
        if not self.ctx.libs.settle_handling():
            successor = self.held.successor.pid if self.held.successor else ""
            self.ctx.ledger.log(f"handling-settle-failed\tturn={turn}\tsuccessor={successor}")
        self.held.arm = self.held.successor
        self.held.successor = None

    def _attended_engine(self, close: Close) -> Engine:
        """The engine an attended close is judged with. A close the supervision
        session may not take reaches main exactly as the plain arm delivers it."""
        acceptance = judge_attended(self.ctx, close.first_wake)
        if acceptance.refusal is not None:
            self.ctx.ledger.log(
                f"pass-through\tattended\t{acceptance.refusal.why}\t{close.first_wake}"
            )
            if self.exits.handback_acknowledged():
                self.exits.acknowledged_handback(close, ())
            if (
                acceptance.refusal.main_only
                and self.cycles.leave_for_main(close.arm_pid, close.first_wake)
                is Left.DOWNTIME_UNRESTORED
            ):
                self.ctx.out.write(_MAIN_ONLY_DOWNTIME_FAILED)
                raise Exit(1)
            self.exits.pass_through(close)
        if not self.ctx.still_owner():
            self.exits.stand_down(close, NO_LONGER_OWNER)
        return acceptance.engine

    def _away_engine(self, close: Close) -> Engine:
        """The engine an away close goes to, once the home can still run its turn."""
        ctx = self.ctx
        if not ctx.still_owner():
            self.exits.stand_down(close, NO_LONGER_OWNER)
        config = ctx.libs.engine_config()
        until = ctx.libs.paused_until(config.engine)
        refusal = decide.away_refusal(
            runs_host=config.runs_host,
            engine=config.engine.name,
            node_present=shutil.which("node", path=ctx.env.get("PATH")) is not None,
            cooling=latch.cooling(until, int(time.time())),
        )
        if refusal is not None:
            clock = ctx.libs.clock(until) if until is not None else ""
            why = decide.away_refusal_line(refusal, problem=config.engine.problem, until=clock)
            self.exits.exit_to_main(close, Handback(why), generation="")
        return config.engine

    def _hand_off_to_successor(self, close: Close) -> str:
        """Start and verify the successor cycle and confirm the handling handoff
        before the turn. The result is the recovery generation it started."""
        verified = self.cycles.start_successor(close.arm_pid)
        if verified is None:
            self.exits.exit_to_main(
                close,
                Handback(
                    "the successor watcher cycle could not be verified before handling; "
                    "this wake is yours"
                ),
                generation="",
            )
        watcher = str(verified.watcher) if verified.watcher is not None else ""
        if verified.generation and (
            self.ctx.run(
                self.ctx.script(
                    "fm-watch-arm.sh",
                    "--handling-delivered",
                    verified.generation,
                    "--watcher-pid",
                    watcher,
                )
            ).rc
            != 0
        ):
            self.exits.exit_to_main(
                close,
                Handback(
                    "the handling handoff to the successor watcher could not be confirmed; "
                    "this wake is yours"
                ),
                generation=verified.generation,
            )
        return verified.generation

    def _keep_for_main(self, close: Close, why: str, generation: str) -> NoReturn:
        """The turn started attended and the supervision session may not take the
        close: main handles it after all."""
        ctx = self.ctx
        ctx.ledger.log(f"pass-through\tattended\t{why}\t{close.first_wake}")
        if self.exits.handback_acknowledged():
            self.exits.acknowledged_handback(close, ())
        # The successor this turn already started and confirmed stays up.
        # Retiring it is what left no watcher after a close that became
        # main-only.
        self.cycles.detach_successor(close.first_wake)
        # Hand back the downtime the handoff consumed: the re-arm owner delivers
        # the close only while the recovery marker reads downtime. It is handed
        # back as already announced, so the successor left running does not
        # resurface the episode this very close delivers.
        if generation and not ctx.libs.hand_back_downtime():
            ctx.ledger.log(f"pass-through\tdowntime-unrestored\t{close.first_wake}")
            ctx.out.write(_MAIN_ONLY_DOWNTIME_FAILED)
            raise Exit(1)
        self.exits.pass_through(close)

    def _after_turn(self, result: TurnResult) -> Handback | None:
        """Where the close goes after its turn, from what the turn left."""
        present = not self.ctx.libs.away_present()
        return decide.after_turn(
            failure=result.why if result.outcome is TurnOutcome.FAILED else None,
            away=result.away,
            returned=self._returned(result, present=present),
            captain_seqs=self._captain_seqs(result.turn) if result.turn and present else "",
            health_note=result.health_note,
        )

    def _returned(self, result: TurnResult, *, present: bool) -> Returned:
        """The visible outcomes an away turn recorded when the captain returned
        during it. A lookup failure is distinct from a turn with no visible
        outcomes."""
        if not result.turn or not result.away or not present:
            return Returned(ReturnedKind.NONE)
        found = receipts.of_turn(self.ctx.paths.receipts, result.turn)
        if found is None:
            return Returned(ReturnedKind.LOOKUP_FAILED)
        seqs = ",".join(receipt.seq for receipt in found)
        return self._look_up(seqs) if seqs else Returned(ReturnedKind.NONE)

    def _look_up(self, seqs: str) -> Returned:
        """The visible outcomes among the outcome store's rows `seqs`."""
        argv = self.ctx.script("fm-branch-outcome.sh", "lookup", "--seqs", seqs)
        looked_up = self.ctx.run(argv, stdout=CAPTURE)
        if looked_up.rc != 0:
            return Returned(ReturnedKind.LOOKUP_FAILED, seqs)
        try:
            rows = text.visible(text.outcome_rows(files.output(looked_up.out)))
        except ValueError:
            return Returned(ReturnedKind.LOOKUP_FAILED, seqs)
        if not rows:
            return Returned(ReturnedKind.NONE, seqs)
        return Returned(
            ReturnedKind.OUTCOMES, seqs, text.outcome_seqs(rows), text.outcome_lines(rows)
        )

    def _captain_seqs(self, turn: str) -> str:
        """The captain outcomes one turn recorded, as store rows."""
        found = receipts.of_turn(self.ctx.paths.receipts, turn) or []
        return ", ".join(receipt.seq for receipt in found if receipt.verdict == "captain")
