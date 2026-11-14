"""Warm-cache compaction while the host is parked.

bin/fm-warm-compact-lib.sh owns the gate and the guards. The park is the
primary's idle episode: the owner starts the host at the primary's turn end
and the host exits when main is needed. The engine conversation's episode is
the time since its last handled turn. Each is compacted at most once per
episode, and neither result reaches the close.
"""

import contextlib
import os
import subprocess
import time
from collections.abc import Callable

from firstmate import files

from . import conversation
from .boundary import ParkBoundary
from .context import HostContext
from .libs import Engine
from .procs import Held, ProcessControl

_DONE = ("sent", "failed")
_ENGINE_DONE = ("compacted", "failed")
_POLL_SECONDS = 0.5


def due(elapsed: int, last_tick: int | None, tick: int) -> bool:
    """True when a compaction check is due: the first one, or `tick` seconds after the last."""
    return last_tick is None or elapsed - last_tick >= tick


class Warm:
    def __init__(
        self, ctx: HostContext, procs: ProcessControl, held: Held, boundary: ParkBoundary
    ) -> None:
        self.ctx = ctx
        self.procs = procs
        self.held = held
        self.boundary = boundary
        self.last_tick: int | None = None
        self.primary_done = False

    def restart(self) -> None:
        """A new park episode: the next check is due at once."""
        self.last_tick = None

    def tick(self, abandon: Callable[[], bool]) -> None:
        """Compact what is due. `abandon` says when a real wake needs the host."""
        ctx = self.ctx
        if not ctx.paths.configured("warm-compact"):
            return
        elapsed = ctx.clock.elapsed()
        if not due(elapsed, self.last_tick, ctx.settings.warm_compact_tick):
            return
        self.last_tick = elapsed
        if self.boundary.turn_crosses():
            return
        self._compact_primary(elapsed)
        self._compact_engine(abandon)

    def _compact_primary(self, elapsed: int) -> None:
        ctx = self.ctx
        transcript = ctx.env.get("FM_WARM_COMPACT_TRANSCRIPT", "")
        if ctx.facts.primary != "claude" or self.primary_done or not transcript:
            return
        target = ctx.libs.supervisor_target()
        if target is None:
            return
        verdict = ctx.libs.warm_compact_primary(*target, elapsed, transcript)
        if verdict in _DONE:
            self.primary_done = True
            ctx.ledger.log(f"warm-compact\tprimary\t{verdict}")

    def _compact_engine(self, abandon: Callable[[], bool]) -> None:
        ctx = self.ctx
        config = ctx.libs.engine_config()
        engine = config.engine
        if not config.runs_host or not engine.name:
            return
        record = conversation.read(ctx.paths.engine_record)
        if not conversation.resumable(
            record,
            key=ctx.libs.main_key(),
            engine=engine,
            rotate_turns=ctx.settings.rotate_turns,
            prompt_ready=files.nonempty(ctx.paths.prompt_file),
        ):
            return
        if record.ended <= 0:
            return
        verdict = self._run_engine(
            engine,
            idle=int(time.time()) - record.ended,
            session=record.session,
            compacted=record.compacted == record.ended,
            abandon=abandon,
        )
        if verdict in _ENGINE_DONE:
            with ctx.stops.shielded():
                conversation.mark_compacted(ctx.paths.engine_record, record.ended)
            ctx.ledger.log(f"warm-compact\tengine\t{verdict}")

    def _run_engine(
        self,
        engine: Engine,
        *,
        idle: int,
        session: str,
        compacted: bool,
        abandon: Callable[[], bool],
    ) -> str:
        """Run the engine compaction as a bounded background turn the cleanup can
        stop, and abandon it the moment `abandon` says so, so a real wake never
        waits behind it. The verdict, empty when abandoned or when the
        compaction could not start."""
        ctx = self.ctx
        try:
            fd, verdict_file = files.mkstemp(ctx.paths.at(".supervision-host-warm.verdict"))
        except OSError:
            return ""
        with ctx.stops.shielded():
            try:
                started = ctx.libs.bash.start(
                    "fm_warm_compact_engine",
                    ctx.paths.config,
                    idle,
                    session,
                    engine.name,
                    engine.model,
                    ctx.paths.prompt_file,
                    1 if compacted else 0,
                    ctx.paths.engine_pid_file,
                    stdout=fd,
                    env=ctx.engine_env(),
                )
            except OSError:
                started = None
            finally:
                os.close(fd)
            self.held.engine = started
        if started is None:
            files.remove(verdict_file)
            return ""
        abandoned = False
        while started.proc.poll() is None:
            if abandon():
                abandoned = True
                self.procs.stop_engine(started)
                break
            with contextlib.suppress(subprocess.TimeoutExpired):
                started.proc.wait(timeout=_POLL_SECONDS)
        started.proc.wait()
        self.held.engine = None
        files.remove(ctx.paths.engine_pid_file)
        verdict = "" if abandoned else files.output(files.read_text(verdict_file) or "")
        files.remove(verdict_file)
        return verdict
