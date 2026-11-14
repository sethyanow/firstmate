"""The processes one host starts, records, and stops."""

import contextlib
import os
import subprocess
import time
from dataclasses import dataclass, field

from firstmate import files
from firstmate.bashlib import Started
from firstmate.processes import Sig
from firstmate.signals import Stops

from . import records
from .libs import HostLibs
from .settings import Paths

_RETIRE_SECONDS = 10
_TURN_REAP_SECONDS = 10


@dataclass(frozen=True)
class Child:
    """A process this host started, with the file that captures its output."""

    started: Started
    out: str

    @property
    def proc(self) -> subprocess.Popen[bytes]:
        return self.started.proc

    @property
    def pid(self) -> int:
        return self.started.pid

    def alive(self) -> bool:
        return self.proc.poll() is None


@dataclass
class Held:
    """What the host holds that its cleanup releases."""

    arm: Child | None = None
    successor: Child | None = None
    # A running engine turn or warm compaction.
    engine: Started | None = None
    grant: bool = False
    # The running turn's result and diagnostics files.
    turn_files: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProcessControl:
    paths: Paths
    libs: HostLibs
    stops: Stops
    engine_grace: int

    def answers_to(self, pid: int | None, identity: str) -> bool:
        """True while `pid` is alive under the recorded `identity`."""
        return (
            pid is not None
            and bool(identity)
            and self.libs.bash.alive(pid)
            and self.libs.identity_of(pid) == identity
        )

    def record(self, role: str, pid: int) -> None:
        """Record one process this host runs, so a successor host can stop exactly it."""
        identity = self.libs.identity_of(pid)
        with self.stops.shielded():
            records.append(self.paths.host_record, role, pid, identity)

    def refresh(self, pid: int) -> None:
        """Re-record this host's own unreaped child under its current identity: its
        first identity may have been read between its fork and its exec."""
        if not os.path.isfile(self.paths.host_record):
            return
        identity = self.libs.identity_of(pid)
        with self.stops.shielded():
            records.refresh(self.paths.host_record, pid, identity)

    def forget(self, pid: int) -> None:
        with self.stops.shielded():
            records.forget(self.paths.host_record, pid)

    def stop_recorded(self, pid: int | None, identity: str, seconds: int) -> None:
        """Stop a recorded process only while it still answers to its recorded
        identity: TERM, then KILL once `seconds` pass."""
        if pid is None or not self.answers_to(pid, identity):
            return
        if not self.libs.bash.send(pid, Sig.TERM):
            return
        deadline = time.monotonic() + seconds
        while self.libs.bash.alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.answers_to(pid, identity):
            self.libs.bash.send(pid, Sig.KILL)

    def retire(self, child: Child | None) -> None:
        """Stop one arm this host started (its TERM handler stops the watcher it
        owns) and drop its row and output file."""
        if child is None:
            return
        if child.alive():
            self.libs.bash.send(child.pid, Sig.TERM)
            try:
                child.proc.wait(timeout=_RETIRE_SECONDS)
            except subprocess.TimeoutExpired:
                self.kill(child.started)
        self.forget(child.pid)
        files.remove(child.out)

    def kill(self, started: Started) -> None:
        """KILL a process this host started and reap it."""
        if not self.libs.bash.send(started.pid, Sig.KILL):
            started.proc.kill()
        started.proc.wait()

    def stop_engine(self, engine: Started) -> None:
        """Stop a running engine turn: TERM the bounded process, whose watchdog
        passes it on and KILLs after its grace, then let the turn's own poller
        reap the engine's descendants before giving up on it. A turn stopped
        before it recorded its bounded process records it while the host waits,
        and that process is stopped then."""
        seen: set[records.PidIdentity | None] = set()
        self._term_recorded_engine(seen)
        deadline = time.monotonic() + self.engine_grace + _TURN_REAP_SECONDS
        while engine.proc.poll() is None and time.monotonic() < deadline:
            with contextlib.suppress(subprocess.TimeoutExpired):
                engine.proc.wait(timeout=0.1)
            self._term_recorded_engine(seen)
        if engine.proc.poll() is None:
            self.kill(engine)

    def _term_recorded_engine(self, seen: set[records.PidIdentity | None]) -> None:
        """TERM the bounded process the engine pid record names while it still
        answers to its identity, judging each record once."""
        record = records.read_pid_identity(self.paths.engine_pid_file)
        if record in seen:
            return
        seen.add(record)
        if record is None or record.pid is None:
            return
        if self.answers_to(record.pid, record.identity):
            self.libs.bash.send(record.pid, Sig.TERM)
