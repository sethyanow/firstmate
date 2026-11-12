"""The bash library functions the host calls, each with Python types at its edge.

firstmate.bashlib runs each call. This module converts each answer once.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from firstmate.bashlib import Bash
from firstmate.processes import CAPTURE, INHERIT, NULL, pid_from

from .settings import Paths

# The libraries the host's functions run with, in the order it sources them.
LIBRARIES = (
    "fm-wake-lib.sh",
    "fm-session-lock-lib.sh",
    "fm-timeout-lib.sh",
    "fm-supervision-engine-lib.sh",
    "fm-afk-contract.sh",
    "fm-backend.sh",
    "fm-supervisor-target-lib.sh",
    "fm-warm-compact-lib.sh",
)

# The identity read runs once a poll while parked, so it sources only what it needs.
IDENTITY_LIBRARIES = ("fm-wake-lib.sh", "fm-supervision-engine-lib.sh")

_ENGINE_VARS = (
    "FM_SUPERVISION_ENGINE",
    "FM_SUPERVISION_ENGINE_MODEL",
    "FM_SUPERVISION_ENGINE_PROBLEM",
)


@dataclass(frozen=True)
class Engine:
    """The home's supervision engine (fm_supervision_host_config): empty name
    and model, with the problem named, when none is usable."""

    name: str = ""
    model: str = ""
    problem: str = ""

    def shell_globals(self) -> dict[str, str]:
        """The globals the engine library's later functions read."""
        return dict(zip(_ENGINE_VARS, (self.name, self.model, self.problem), strict=True))


@dataclass(frozen=True)
class EngineConfig:
    runs_host: bool
    engine: Engine


@dataclass(frozen=True)
class Readiness:
    """fm_supervision_host_attended_ready: whether the attended host is ready,
    why not, and the engine it read."""

    ready: bool
    unready: str
    engine: Engine


@dataclass(frozen=True)
class StartupValues:
    """What the host reads from the libraries once, before it is built."""

    grace: str
    cooldown: int
    primary: str


def _engine(values: Mapping[str, str]) -> Engine:
    return Engine(*(values.get(name, "") for name in _ENGINE_VARS))


def _digits(text: str) -> int | None:
    return int(text) if text.isascii() and text.isdigit() else None


def startup_values(bash: Bash, env: Mapping[str, str]) -> StartupValues:
    """The poll-derived guard grace, the latch's first cooldown, and the primary,
    each unless the environment names it."""
    return StartupValues(
        grace=env.get("FM_GUARD_GRACE") or bash.call("fm_poll_derived_grace").text,
        cooldown=int(bash.value("FM_SUPERVISION_HOST_COOLDOWN")),
        primary=env.get("FM_SUPERVISION_HOST_PRIMARY")
        or bash.call("fm_supervision_host_primary").text,
    )


@dataclass(frozen=True)
class HostLibs:
    """The library calls one host makes, for its home and primary."""

    bash: Bash
    paths: Paths
    primary: str

    # --- ownership and posture -------------------------------------------------

    def session_owned(self) -> bool:
        return self.bash.call("fm_session_lock_owned_by_self", self.paths.state, stdout=INHERIT).ok

    def autoarm_current(self, gen: str, owner: int | None) -> bool:
        """True while the auto-arm ledger still names this generation, owned by
        `owner`, as arming."""
        ledger = self.bash.call(
            "fm_autoarm_ledger_read",
            self.paths.state,
            stdout=INHERIT,
            out_vars=("FM_AUTOARM_GEN", "FM_AUTOARM_OWNER", "FM_AUTOARM_OUTCOME"),
        )
        return (
            ledger.ok
            and ledger.values["FM_AUTOARM_GEN"] == gen
            and pid_from(ledger.values["FM_AUTOARM_OWNER"]) == owner
            and ledger.values["FM_AUTOARM_OUTCOME"] == "arming"
        )

    def away_present(self) -> bool:
        return self.bash.call("fm_afk_contract_away_present", self.paths.state, stdout=INHERIT).ok

    def main_key(self) -> str | None:
        result = self.bash.call("fm_supervision_host_main_key", self.paths.state)
        return result.text if result.ok else None

    # --- the engine and its latch ------------------------------------------------

    def engine_config(self) -> EngineConfig:
        result = self.bash.call(
            "fm_supervision_host_config",
            self.paths.config,
            self.primary,
            stdout=INHERIT,
            out_vars=_ENGINE_VARS,
        )
        return EngineConfig(result.ok, _engine(result.values))

    def attended_readiness(self) -> Readiness:
        result = self.bash.call(
            "fm_supervision_host_attended_ready",
            self.paths.config,
            self.primary,
            stdout=INHERIT,
            out_vars=("FM_SUPERVISION_HOST_UNREADY", *_ENGINE_VARS),
        )
        return Readiness(
            result.ok, result.values["FM_SUPERVISION_HOST_UNREADY"], _engine(result.values)
        )

    def health_key(self, engine: Engine) -> str | None:
        result = self.bash.with_globals(engine.shell_globals()).call(
            "fm_supervision_host_health_key", self.paths.state
        )
        return result.text if result.ok else None

    def paused_until(self, engine: Engine) -> int | None:
        """The epoch the latch's next probe is due, while the latch holds."""
        result = self.bash.with_globals(engine.shell_globals()).call(
            "fm_supervision_host_paused_until", self.paths.state
        )
        return _digits(result.text) if result.ok else None

    def clock(self, epoch: int) -> str:
        return self.bash.call("fm_supervision_host_clock", epoch).text

    def local_time(self) -> str:
        """The current time as wake notices show it (fm_wake_local_time)."""
        return self.bash.call("fm_wake_local_time", "").text

    def timed_out(self, rc: int) -> bool:
        return self.bash.call("fm_timed_out", rc, stdout=NULL).ok

    def engine_result(self, engine: Engine, result_file: str, cost: str) -> str:
        """The usage line of a finished turn (fm_supervision_engine_result)."""
        return self.bash.call(
            "fm_supervision_engine_result", engine.name, result_file, cost, stderr=NULL
        ).text

    # --- processes -----------------------------------------------------------------

    def identity_of(self, pid: int) -> str:
        """A process's identity (bin/fm-wake-lib.sh fm_pid_identity)."""
        return self.bash.sourcing(IDENTITY_LIBRARIES).call("_fm_engine_identity", pid).text

    def reap_descendants(self, ledger: str) -> None:
        self.bash.call("_fm_engine_reap", ledger, stdout=INHERIT)

    # --- the wake queue and recovery marker ----------------------------------------

    def publish_downtime(self, *, quiet: bool = True) -> bool:
        return self.bash.call(
            "fm_recovery_marker_publish",
            self.paths.marker,
            "downtime",
            stdout=NULL,
            stderr=NULL if quiet else INHERIT,
        ).ok

    def hand_back_downtime(self) -> bool:
        return self.bash.call(
            "fm_recovery_marker_handback", self.paths.marker, stdout=NULL, stderr=NULL
        ).ok

    def settle_handling(self) -> bool:
        return self.bash.call(
            "fm_recovery_marker_handling_settled", self.paths.marker, stdout=INHERIT
        ).ok

    def rows_queued(self, rows: Sequence[str]) -> str | None:
        """The granted rows the queue still holds, one per line, or None when it
        cannot be read."""
        result = self.bash.call("fm_wake_rows_queued", *rows, stdout=CAPTURE)
        return result.out if result.ok else None

    def append_wake(self, kind: str, key: str, line: str) -> bool:
        return self.bash.call("fm_wake_append", kind, key, line, stdout=INHERIT).ok

    def watcher_healthy(self, grace: str) -> bool:
        return self.bash.call(
            "fm_watcher_healthy",
            self.paths.state,
            self.paths.script("fm-watch.sh"),
            grace,
            self.paths.home,
            stdout=INHERIT,
        ).ok

    # --- warm compaction -------------------------------------------------------------

    def supervisor_target(self) -> tuple[str, str] | None:
        """The primary's backend and target, or None when either is unknown."""
        target = self.bash.call("discover_supervisor_target")
        if not target.ok:
            return None
        backend = self.bash.call("discover_supervisor_backend")
        return (backend.text, target.text) if backend.ok else None

    def warm_compact_primary(
        self, backend: str, target: str, elapsed: int, transcript: str
    ) -> str:
        return self.bash.call(
            "fm_warm_compact_primary",
            self.paths.config,
            backend,
            target,
            elapsed,
            transcript,
            0,
        ).text
