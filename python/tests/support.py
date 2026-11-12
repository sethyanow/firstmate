"""Fixture homes and processes for the host's tests.

Fixture processes block on a FIFO read, so nothing waits on a clock; a wait
below only guards against a fixture that never gets ready.
"""

import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from firstmate.host import Host, assemble
from firstmate.processes import pid_alive
from firstmate.signals import Stops

REPO = Path(__file__).resolve().parents[2]
BIN = str(REPO / "bin")
HANG_GUARD = 60

# A process that reports a TERM by writing "term" and exiting with the given
# trap's status, then blocks reading its FIFO until released.
_BLOCKER = r"""
trap "$1" TERM
exec 9<> "$2"
: > "$3"
read -r -u 9 _
printf 'released\n' > "$4"
"""


def until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + HANG_GUARD
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"fixture never reached: {what}")
        time.sleep(0.02)


def write(path: str, content: str) -> None:
    with open(path, "w") as handle:
        handle.write(content)


def run_python(code: str, *, ignored: str = "") -> tuple[int, str]:
    """Run `code` in a fresh interpreter started with the `ignored` signals
    ignored; its exit status and output. A hang fails the test."""
    launcher = f'trap \'\' {ignored}; exec "$0" -I -c "$1"' if ignored else 'exec "$0" -I -c "$1"'
    proc = subprocess.run(
        ["bash", "-c", launcher, sys.executable, code],
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


class Blocker:
    def __init__(self, home: str, index: int) -> None:
        self.pid = 0
        self.fifo = os.path.join(home, f"block-{index}")
        self.ready = os.path.join(home, f"ready-{index}")
        self.released = os.path.join(home, f"released-{index}")
        self.term = os.path.join(home, f"term-{index}")

    def release(self) -> None:
        """Let the blocker finish; a blocker that is gone has nothing to release."""
        try:
            fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            os.write(fd, b"go\n")
        finally:
            os.close(fd)

    def wait_gone(self) -> None:
        until(lambda: not pid_alive(self.pid), f"process {self.pid} gone")

    @property
    def saw_term(self) -> bool:
        return os.path.exists(self.term)

    @property
    def finished_normally(self) -> bool:
        return os.path.exists(self.released)


class Home:
    """A fixture home and hosts on it, as the host's own process sees them."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="fm-host-unit.")
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "state"))
        os.makedirs(os.path.join(self.home, "config"))
        self.blockers: list[Blocker] = []

    @property
    def state(self) -> str:
        return os.path.join(self.home, "state")

    def at(self, name: str) -> str:
        return os.path.join(self.state, name)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("FM_STATE_OVERRIDE", "FM_CONFIG_OVERRIDE", "FM_ROOT_OVERRIDE"))
        }
        env.update(
            FM_HOME=self.home,
            FM_SUPERVISION_HOST_PRIMARY="claude",
            FM_GUARD_GRACE="300",
            FM_SUPERVISION_ENGINE_GRACE="1",
        )
        env.pop("FM_SUPERVISION_ACTOR", None)
        env.update(extra)
        return env

    def host(self, script_dir: str = BIN, stops: Stops | None = None, **extra: str) -> Host:
        return assemble(self.env(**extra), script_dir, stops or Stops(), restart=False)

    def blocker(self, trap: str = "printf 'term\\n' > \"$5\"; exit 42") -> Blocker:
        """Start a blocked process that is not this test's child; its Blocker."""
        blocker = Blocker(self.home, len(self.blockers))
        os.mkfifo(blocker.fifo)
        launcher = subprocess.run(
            [
                "bash",
                "-c",
                'bash -c "$1" _ "$2" "$3" "$4" "$5" "$6" </dev/null >/dev/null 2>&1 & '
                'printf "%s\\n" "$!"',
                "_",
                _BLOCKER,
                trap,
                blocker.fifo,
                blocker.ready,
                blocker.released,
                blocker.term,
            ],
            stdout=subprocess.PIPE,
            check=True,
            text=True,
        )
        blocker.pid = int(launcher.stdout.strip())
        until(lambda: os.path.exists(blocker.ready), "blocker ready")
        self.blockers.append(blocker)
        return blocker

    def cleanup(self) -> None:
        for blocker in self.blockers:
            if pid_alive(blocker.pid):
                os.kill(blocker.pid, signal.SIGKILL)
        self.tmp.cleanup()
