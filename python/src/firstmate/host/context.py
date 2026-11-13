"""What every part of one host shares: its environment, paths, settings, facts,
clock, stop handling, library calls, ledger, and output."""

import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from firstmate import files
from firstmate.processes import NULL, Command, Completed, Stream, run
from firstmate.signals import Stop, Stops

from .libs import HostLibs
from .records import Ledger
from .settings import Paths, Settings


@dataclass(frozen=True)
class Facts:
    """Who this host is and whom it serves, fixed for its whole park."""

    pid: int
    gen: str
    primary: str
    grace: str
    cooldown: int
    restart: bool
    # The auto-arm generation and owner pid this host serves, when the auto-arm
    # launched it.
    autoarm_gen: str
    autoarm_owner: int | None
    # The owner's predecessor arm, which belongs to the first cycle only.
    owner_predecessor: int | None


class ParkClock(Protocol):
    def elapsed(self) -> int:
        """Whole seconds since the park started or last restarted."""
        ...

    def restart(self) -> None: ...


class MonotonicClock:
    """The park's clock: monotonic, so wall-clock changes do not move it."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    def elapsed(self) -> int:
        return int(time.monotonic() - self._started)

    def restart(self) -> None:
        self._started = time.monotonic()


@dataclass(frozen=True)
class Output:
    """The host's standard output, which its owner reads."""

    stops: Stops
    fd: int = 1

    def write(self, text: str) -> None:
        """Write in one call where the pipe allows, never split by a stop signal.
        An owner that closed its end stops the host, as SIGPIPE stopped bash
        through its cleanup."""
        data = files.encode(text)
        with self.stops.shielded():
            try:
                while data:
                    data = data[os.write(self.fd, data) :]
            except BrokenPipeError:
                raise Stop(128 + signal.SIGPIPE) from None


@dataclass(frozen=True)
class HostContext:
    env: Mapping[str, str]
    paths: Paths
    settings: Settings
    facts: Facts
    stops: Stops
    libs: HostLibs
    ledger: Ledger
    clock: ParkClock
    out: Output

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdout: Stream = NULL,
        input_text: str | None = None,
    ) -> Completed:
        """Run a command in the foreground in the host's environment."""
        command = Command(argv, self.env if env is None else env, stdout, input_text)
        return run(command, self.stops)

    def private_run(
        self, argv: Sequence[str], out_path: str, input_text: str | None = None
    ) -> int:
        """Run a command whose output may carry the captain's words into an owner-only file."""
        try:
            fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError:
            return 1
        try:
            return run(Command(argv, self.env, fd, input_text, umask=0o077), self.stops).rc
        finally:
            os.close(fd)

    def script(self, name: str, *args: str) -> list[str]:
        """A bin shell script's command, through bash: Windows does not read
        the script's shebang."""
        return ["bash", self.paths.script(name), *args]

    def node_script(self, name: str, *args: str) -> list[str]:
        return ["node", self.paths.script(name), *args]

    def engine_env(self) -> dict[str, str]:
        """An engine run's environment. Its bound (fm_exec_timed) stops the engine
        once this host is gone, as it did when `$$` named the host."""
        return {
            **self.env,
            "FM_HOME": self.paths.home,
            "STATE": self.paths.state,
            "FM_EXEC_TIMED_OWNER_PID": str(self.facts.pid),
        }

    def grant(self, *args: str) -> int:
        """Run bin/fm-wake-grant.sh and return its exit status."""
        return self.run(self.script("fm-wake-grant.sh", *args)).rc

    def release_grant(self) -> None:
        self.grant("release", self.facts.gen)

    def release_branch_leases(self) -> None:
        self.run(
            self.script("fm-lease.sh", "release-actor", "--actor", "branch"),
            env={**self.env, "FM_SUPERVISION_ACTOR": "branch"},
        )

    def stop_watcher(self) -> None:
        self.run(self.script("fm-watch-arm.sh", "--stop"))

    def still_owner(self) -> bool:
        """True while this session holds the fleet lock and, for a host the
        auto-arm launched, the generation it serves is still current."""
        if not self.libs.session_owned():
            return False
        if not self.facts.autoarm_gen:
            return True
        return self.libs.autoarm_current(self.facts.autoarm_gen, self.facts.autoarm_owner)
