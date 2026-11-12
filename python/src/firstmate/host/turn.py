"""One close handled on the engine: the posture check, the grant, one bounded
engine turn, and the judgment of what it did (the firstmate.host docstring
owns the order)."""

import enum
import os
import time
import uuid
from dataclasses import dataclass

from firstmate import files
from firstmate.processes import CAPTURE, exit_status

from . import conversation, decide, latch, receipts, scope, text
from .boundary import ParkBoundary
from .context import HostContext
from .conversation import Conversation, Mode
from .exits import NO_LONGER_OWNER
from .libs import Engine
from .procs import Held

_MIRROR_UNREADABLE = "the dialog mirror could not be read"
# fm-branch-dispatch.mjs wake-prompt's status for an unreadable mirror feed.
_PROMPT_MIRROR_RC = 3
# fm-wake-grant.sh publish's status when main already claimed the rows.
_GRANT_CLAIMED_RC = 3


class TurnOutcome(enum.Enum):
    HANDLED = enum.auto()
    # The scan held nothing the branch may claim: handled without a turn.
    NO_CLAIM = enum.auto()
    # The turn started attended and the supervision session may not take the close.
    KEPT_FOR_MAIN = enum.auto()
    FAILED = enum.auto()
    # A turn started now could outlive the park boundary.
    BOUNDARY = enum.auto()


@dataclass(frozen=True)
class TurnResult:
    outcome: TurnOutcome
    # Why main has the wake (FAILED), or why the close stays main's (KEPT_FOR_MAIN).
    why: str = ""
    # The turn that ran, once one started, and whether it started away.
    turn: str = ""
    away: bool = False
    # The line main is owed when this turn tripped the latch.
    health_note: str = ""


@dataclass(frozen=True)
class Acceptance:
    """An attended close judged: why it stays main's (None when the supervision
    session may take it), the offer's scope, and the engine read."""

    refusal: str | None
    offer: scope.Scope
    engine: Engine


def judge_attended(ctx: HostContext, first: str) -> Acceptance:
    """Whether the supervision session may take an attended close that the plain
    arm would deliver to main."""
    readiness = ctx.libs.attended_readiness()
    engine = readiness.engine
    refusal = decide.attended_refusal(
        ready=readiness.ready,
        unready=readiness.unready,
        main_known=ctx.libs.main_key() is not None,
        cooling=latch.cooling(ctx.libs.paused_until(engine), int(time.time())),
    )
    if refusal is not None:
        return Acceptance(refusal, scope.parse(""), engine)
    offered = ctx.run(
        ctx.node_script("fm-branch-dispatch.mjs", "offer"),
        stdout=CAPTURE,
        input_text=first + "\n",
    )
    offer = scope.parse(files.output(offered.out))
    return Acceptance(decide.offer_refusal(offered.rc, offer), offer, engine)


class _NoTurnError(Exception):
    """A close that ends without an engine turn, with its result."""

    def __init__(self, result: TurnResult) -> None:
        super().__init__(result.outcome.name)
        self.result = result


def _failed(why: str) -> _NoTurnError:
    return _NoTurnError(TurnResult(TurnOutcome.FAILED, why))


@dataclass(frozen=True)
class _Claim:
    """The rows a turn may claim, in the posture and with the engine it starts in."""

    scope: scope.Scope
    away: bool
    engine: Engine


@dataclass(frozen=True)
class _Prepared:
    turn: str
    conversation: Conversation


@dataclass(frozen=True)
class _EngineRun:
    rc: int
    result_file: str | None
    errors_file: str | None


class Turns:
    def __init__(self, ctx: HostContext, held: Held, boundary: ParkBoundary) -> None:
        self.ctx = ctx
        self.held = held
        self.boundary = boundary
        self.seq = 0

    def handle(self, reason: str, engine: Engine) -> TurnResult:
        """Handle one close on the engine, in the posture the record gives when the
        turn starts. `engine` is the engine the close was judged with."""
        first = text.first(reason)
        try:
            claim = self._claim(first, engine)
            self._publish_grant(claim.scope.rows)
            prepared = self._prepare_or_abandon(reason, claim)
        except _NoTurnError as no_turn:
            return no_turn.result
        return self._run(prepared, claim, first)

    def _claim(self, first: str, engine: Engine) -> _Claim:
        """The rows the branch may claim in the turn's posture."""
        ctx = self.ctx
        away = ctx.libs.away_present()
        if away:
            args = ["--heartbeat"] if first.startswith("heartbeat") else []
            argv = ctx.node_script("fm-branch-dispatch.mjs", "scope", *args, "--afk")
            scoped = ctx.run(argv, stdout=CAPTURE)
            if scoped.rc != 0:
                raise _failed("branch eligibility could not be computed")
            claim = _Claim(scope.parse(files.output(scoped.out)), away, engine)
        else:
            acceptance = judge_attended(ctx, first)
            if acceptance.refusal is not None:
                raise _NoTurnError(TurnResult(TurnOutcome.KEPT_FOR_MAIN, acceptance.refusal))
            claim = _Claim(acceptance.offer, away, acceptance.engine)
        if claim.scope.corrupted:
            raise _failed(
                "a queued wake could not be read or resolved to a task record, so its scope "
                "is unknown"
            )
        if not claim.scope.claimable:
            ctx.ledger.log(f"no-op\tnothing for the branch to claim\t{first}")
            raise _NoTurnError(TurnResult(TurnOutcome.NO_CLAIM))
        return claim

    def _publish_grant(self, rows: tuple[str, ...]) -> None:
        """Activate this host's grant once and publish the rows the turn claims."""
        ctx = self.ctx
        if not self.held.grant:
            if ctx.grant("activate", str(ctx.facts.pid), ctx.facts.gen) != 0:
                raise _failed("the branch grant could not be activated")
            self.held.grant = True
        rc = ctx.grant("publish", ctx.facts.gen, *rows)
        if rc == _GRANT_CLAIMED_RC:
            raise _failed("main already claimed these wake rows")
        if rc != 0:
            raise _failed("the branch grant could not be published")

    def _prepare_or_abandon(self, reason: str, claim: _Claim) -> _Prepared:
        """Prepare the turn; a turn that will not run gives its records and rows back."""
        try:
            prepared = self._prepare(reason, claim)
            if self.boundary.turn_crosses():
                raise _NoTurnError(TurnResult(TurnOutcome.BOUNDARY))
        except _NoTurnError:
            files.remove(self.ctx.paths.turn_file, self.ctx.paths.mirror_feed)
            self.ctx.release_grant()
            raise
        return prepared

    # --- before the turn ------------------------------------------------------------

    def _prepare(self, reason: str, claim: _Claim) -> _Prepared:
        """Write the turn's records and render its wake."""
        ctx, paths = self.ctx, self.ctx.paths
        if not ctx.still_owner():
            raise _failed(NO_LONGER_OWNER)
        chosen = self._conversation(claim.engine)
        if chosen is None:
            raise _failed("the branch prompt or engine conversation could not be prepared")
        self.seq += 1
        turn = f"{ctx.facts.gen}.{self.seq}"
        claimed = claim.scope
        with ctx.stops.shielded():
            receipts.clear(paths.receipts)
            files.write(
                paths.turn_file,
                f"turn={turn}\nrows={' '.join(claimed.rows)}\ntasks={claimed.tasks}\n"
                f"unscoped={claimed.unscoped or 0}\nwake={text.first(reason)}\n"
                f"posture={_posture(away=claim.away)}\n",
            )
        readback = self._readback() if claim.away else None
        try:
            prompt_args = ["--report", "the bin/fm-branch-report.sh command"]
            if claim.away:
                prompt_args += ["--away", *(["--readback-file", readback] if readback else [])]
            else:
                prompt_args += ["--mirror-file", self._mirror_feed(chosen)]
            self._render_wake(reason, prompt_args)
        finally:
            files.remove(readback)
        files.remove(paths.mirror_feed)
        return _Prepared(turn, chosen)

    def _conversation(self, engine: Engine) -> Conversation | None:
        """The engine conversation for this turn: the recorded one while it belongs
        to this main session and has turns left, otherwise a new one."""
        ctx, paths = self.ctx, self.ctx.paths
        key = ctx.libs.main_key()
        record = conversation.read(paths.engine_record)
        if conversation.resumable(
            record,
            key=key,
            engine=engine,
            rotate_turns=ctx.settings.rotate_turns,
            prompt_ready=os.path.isfile(paths.prompt_file)
            and os.path.getsize(paths.prompt_file) > 0,
        ):
            return conversation.resumed(record)
        try:
            fd, tmp = files.mkstemp(paths.prompt_file + ".tmp")
        except OSError:
            return None
        with ctx.stops.shielded():
            rendered = ctx.run(ctx.script("fm-branch-prompt.sh"), stdout=fd)
            os.close(fd)
            if rendered.rc != 0 or os.path.getsize(tmp) < conversation.PROMPT_MIN_BYTES:
                files.remove(tmp)
                return None
            try:
                os.replace(tmp, paths.prompt_file)
            except OSError:
                files.remove(tmp)
                return None
        return Conversation(str(uuid.uuid4()), Mode.NEW, 0, "0", key or "")

    def _readback(self) -> str | None:
        """The away contract's readback for the prompt, in a private file."""
        ctx = self.ctx
        try:
            fd, path = files.mkstemp(ctx.paths.at(".supervision-host-readback"))
        except OSError:
            return None
        try:
            done = ctx.run(
                ctx.script("fm-afk-contract.sh", "readback"),
                stdout=fd,
                env={**ctx.env, "FM_STATE_OVERRIDE": ctx.paths.state},
            )
            if done.rc != 0:
                os.ftruncate(fd, 0)
        finally:
            os.close(fd)
        return path

    def _mirror_feed(self, chosen: Conversation) -> str:
        """Render the dialog mirror (bin/fm-host-mirror.sh) that rides at the head of
        an attended wake. The engine never judges without the captain's words, so
        a feed that cannot be read hands the wake to main. An away wake never
        reads the mirror or moves its cursor."""
        ctx, feed = self.ctx, self.ctx.paths.mirror_feed
        files.remove(feed)
        argv = ctx.script("fm-host-mirror.sh", "feed", chosen.session, chosen.mode.value)
        if ctx.private_run(argv, feed) != 0:
            raise _failed(_MIRROR_UNREADABLE)
        return feed

    def _render_wake(self, reason: str, prompt_args: list[str]) -> None:
        ctx, wake = self.ctx, self.ctx.paths.wake_file
        files.remove(wake)
        rc = ctx.private_run(
            ctx.node_script("fm-branch-dispatch.mjs", "wake-prompt", *prompt_args),
            wake,
            input_text=reason + "\n",
        )
        if rc == _PROMPT_MIRROR_RC:
            raise _failed(_MIRROR_UNREADABLE)
        if rc != 0:
            raise _failed("the wake prompt could not be rendered")

    # --- the turn -------------------------------------------------------------------

    def _run(self, prepared: _Prepared, claim: _Claim, first: str) -> TurnResult:
        """Run the turn, release what it claimed, and judge it."""
        ctx, paths, turn, engine = self.ctx, self.ctx.paths, prepared.turn, claim.engine
        rows = claim.scope.rows
        ran = self._run_engine(prepared, engine)
        ctx.release_branch_leases()
        unacked = text.unacknowledged(rows, ctx.libs.rows_queued(rows))
        ctx.release_grant()
        files.remove(paths.turn_file)
        usage = ""
        if ran.result_file:
            usage = ctx.libs.engine_result(engine, ran.result_file, prepared.conversation.cost)
        files.remove(ran.result_file)
        end = decide.TurnEnd(
            rc=ran.rc,
            timed_out=ran.rc != 0 and ctx.libs.timed_out(ran.rc),
            usage=usage,
            receipts=len(receipts.of_turn(paths.receipts, turn) or []),
            unacked=unacked,
        )
        health_note = self._record_health(engine, end)
        row = (
            f"turn={turn}\tposture={_posture(away=claim.away)}\trc={end.rc}"
            f"\treports={end.receipts}"
        )
        if end.handled:
            self._keep_conversation(prepared.conversation, engine, usage)
            if not claim.away:
                ctx.run(ctx.script("fm-host-mirror.sh", "commit"))
            ctx.ledger.log(f"handled\t{row}\t{usage}\t{first}")
            self._drop_turn_files(ran)
            return TurnResult(
                TurnOutcome.HANDLED, turn=turn, away=claim.away, health_note=health_note
            )
        # A turn that did not handle its wake starts the next one on a new
        # conversation, so whatever went wrong in this one is not carried forward.
        files.remove(paths.engine_record)
        ctx.ledger.log(
            f"failed\t{row}\tunacked={' '.join(unacked) or 'none'}\t{usage or 'no-result'}"
            f"\t{_diagnostics(ran.errors_file)}\t{first}"
        )
        self._drop_turn_files(ran)
        why = decide.turn_failure(end, turn_timeout=ctx.settings.turn_timeout, engine=engine.name)
        return TurnResult(TurnOutcome.FAILED, why, turn, claim.away, health_note)

    def _run_engine(self, prepared: _Prepared, engine: Engine) -> _EngineRun:
        """One bounded engine turn, run in the background so a stop signal is
        handled at once; the cleanup stops the engine."""
        ctx, paths, chosen = self.ctx, self.ctx.paths, prepared.conversation
        with ctx.stops.shielded():
            result_file = files.scratch(paths.at(".supervision-host-result"))
            errors_file = files.scratch(paths.at(".supervision-host-errors"))
            self.held.turn_files = tuple(path for path in (result_file, errors_file) if path)
            env = {
                **ctx.engine_env(),
                "FM_SUPERVISION_ACTOR": "branch",
                "FM_LEASE_HOLDER_PID": files.first_line(paths.session_lock),
                "FM_SUPERVISION_PRIMARY_HARNESS": ctx.facts.primary,
                "FM_BRANCH_REPORT_TURN": prepared.turn,
            }
            proc = ctx.libs.bash.start(
                "fm_supervision_engine_turn",
                engine.name,
                engine.model,
                paths.prompt_file,
                paths.wake_file,
                chosen.session,
                chosen.mode.value,
                ctx.settings.turn_timeout,
                result_file or os.devnull,
                errors_file or os.devnull,
                paths.engine_pid_file,
                env=env,
            )
            self.held.engine = proc
        rc = exit_status(proc.wait())
        self.held.engine = None
        return _EngineRun(rc, result_file, errors_file)

    def _drop_turn_files(self, ran: _EngineRun) -> None:
        files.remove(ran.result_file, ran.errors_file)
        self.held.turn_files = ()

    def _keep_conversation(self, chosen: Conversation, engine: Engine, usage: str) -> None:
        """Record the conversation a handled turn ran in, so the next turn resumes it."""
        record = conversation.EngineRecord(
            engine=engine.name,
            model=engine.model,
            session=chosen.session,
            key=chosen.key,
            turns=chosen.turns + 1,
            cost=_usage_field(usage, "conversation_cost"),
            ended=int(time.time()),
        )
        with self.ctx.stops.shielded():
            if not conversation.write(self.ctx.paths.engine_record, record):
                files.remove(self.ctx.paths.engine_record)

    def _record_health(self, engine: Engine, end: decide.TurnEnd) -> str:
        """Fold the turn into the latch; the line main is owed when it first trips."""
        ctx = self.ctx
        key = ctx.libs.health_key(engine)
        change = latch.after_turn(
            latch.load(ctx.paths.health_file, key),
            engine_error=end.engine_error,
            reports=end.receipts,
            now=int(time.time()),
            first_cooldown=ctx.facts.cooldown,
        )
        for line in change.logged:
            ctx.ledger.log(line)
        with ctx.stops.shielded():
            latch.save(ctx.paths.health_file, key, change.latch)
        return change.note


def _posture(*, away: bool) -> str:
    return "away" if away else "attended"


def _usage_field(usage: str, name: str) -> str:
    marker = f" {name}="
    if marker not in usage:
        return ""
    return usage.rsplit(marker, 1)[1].split(" ", 1)[0]


def _diagnostics(path: str | None) -> str:
    """The first 300 bytes of a turn's diagnostics on one line."""
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            data = handle.read(300)
    except OSError:
        return ""
    return files.decode(data).replace("\t", " ").replace("\n", " ")
