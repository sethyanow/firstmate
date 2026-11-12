"""Stop signals for a converted script's process.

A handled TERM, HUP, or INT ends the process with 128 plus the signal number,
the status the bash scripts' `trap 'exit 143' TERM` handlers gave, by raising
Stop wherever the process is.
Inside shielded(), the Stop waits until the shielded work is done, as bash ran
a trap only after its foreground command finished: a child command that holds
a lock releases it, and a child that was just started is recorded before the
Stop can skip past it.
Foreground work that is still running `grace` seconds after the stop arrived
is ended (firstmate.processes.finish), so a stop always ends the process.
Python runs the handler between bytecodes, so a signal is never lost the way
bash 5.2 loses a trap that arrives while it parses a command substitution.
A stop signal the process started with ignored stays ignored, because bash
cannot trap a signal that was ignored when the script started (INT for a
background command of a non-interactive shell, HUP under nohup).
"""

import contextlib
import signal
import time
from collections.abc import Generator
from types import FrameType

STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
# Long enough for a call that holds a lock to finish, short enough that a call
# waiting on a wedged lock holder cannot hold a stop off.
STOP_GRACE_SECONDS = 5.0


class Stop(BaseException):
    """A stop signal arrived: end with `status` after cleanup."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class Exit(BaseException):
    """End the process with `status` after cleanup, as a bash `exit` did."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _ignore(_signum: int, _frame: FrameType | None) -> None:
    """Ignore a signal while children still start with its default action."""


def start_as_bash() -> None:
    """Take the dispositions a bash script starts with.

    An unhandled INT ends the process, unless it started ignored.
    QUIT is ignored, as bash always ignores it, through a handler, so the
    process's children start with the default action as a bash script's do.
    """
    if signal.getsignal(signal.SIGINT) is signal.default_int_handler:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    if signal.getsignal(signal.SIGQUIT) == signal.SIG_DFL:
        signal.signal(signal.SIGQUIT, _ignore)


class Stops:
    """One process's stop-signal handling."""

    def __init__(self, grace: float = STOP_GRACE_SECONDS) -> None:
        self.grace = grace
        self._pending: int | None = None
        self._since = 0.0
        self._depth = 0
        self._handled: tuple[signal.Signals, ...] = ()

    def handle(self, signum: int, _frame: FrameType | None = None) -> None:
        if self._pending is None:
            self._pending = 128 + signum
            self._since = time.monotonic()
        if self._depth == 0:
            raise Stop(self._pending)

    def overdue(self) -> bool:
        """True once a pending stop has waited its grace for foreground work."""
        return self._pending is not None and time.monotonic() - self._since >= self.grace

    def install(self) -> None:
        """Handle every stop signal the process did not start with ignored."""
        self._handled = tuple(
            sig for sig in STOP_SIGNALS if signal.getsignal(sig) != signal.SIG_IGN
        )
        for sig in self._handled:
            signal.signal(sig, self.handle)

    def disarm(self) -> None:
        """Let a further stop signal end the process at once, as during cleanup."""
        for sig in self._handled:
            signal.signal(sig, signal.SIG_DFL)
        self._pending = None

    @contextlib.contextmanager
    def shielded(self) -> Generator[None]:
        """Defer a stop signal's Stop until the block is done, however it ends."""
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0 and self._pending is not None:
                raise Stop(self._pending)
