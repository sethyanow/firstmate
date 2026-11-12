"""Stop signals for a converted script's process.

A handled TERM, HUP, or INT ends the process with 128 plus the signal number,
the status the bash scripts' `trap 'exit 143' TERM` handlers gave, by raising
Stop wherever the process is.
Inside shielded(), the Stop waits until the shielded work is done, as bash ran
a trap only after its foreground command finished: a child command that holds
a lock releases it, and a child that was just started is recorded before the
Stop can skip past it.
Python runs the handler between bytecodes, so a signal is never lost the way
bash 5.2 loses a trap that arrives while it parses a command substitution.
A stop signal the process started with ignored stays ignored, because bash
cannot trap a signal that was ignored when the script started (INT for a
background command of a non-interactive shell, HUP under nohup).
"""

import contextlib
import signal

STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class Stop(BaseException):
    """A stop signal arrived: end with `status` after cleanup."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


class Exit(BaseException):
    """End the process with `status` after cleanup, as a bash `exit` did."""

    def __init__(self, status):
        super().__init__(status)
        self.status = status


class Stops:
    """One process's stop-signal handling."""

    def __init__(self):
        self.pending = None
        self._depth = 0
        self._handled = ()

    def handle(self, signum, _frame=None):
        if self.pending is None:
            self.pending = 128 + signum
        if self._depth == 0:
            raise Stop(self.pending)

    def install(self):
        """Handle every stop signal the process did not start with ignored."""
        self._handled = tuple(sig for sig in STOP_SIGNALS if signal.getsignal(sig) != signal.SIG_IGN)
        for sig in self._handled:
            signal.signal(sig, self.handle)

    def disarm(self):
        """Let a further stop signal end the process at once, as during cleanup."""
        for sig in self._handled:
            signal.signal(sig, signal.SIG_DFL)
        self.pending = None

    @contextlib.contextmanager
    def shielded(self):
        """Defer a stop signal's Stop until the block is done."""
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
        if self._depth == 0 and self.pending is not None:
            raise Stop(self.pending)


STOPS = Stops()


def exit_status(returncode):
    """A child's exit status as a shell reports it: 128 plus the signal number
    for a child a signal ended, where subprocess gives the negative number."""
    return 128 - returncode if returncode < 0 else returncode
