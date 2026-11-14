"""Processes as the bash libraries know them: by the pid `$$` reports, which on
native Windows is a Git Bash pid, not the Windows one."""

import os
import signal
import tempfile
import threading
import unittest

from support import BIN, HANG_GUARD, until

from firstmate.bashlib import Bash, Started
from firstmate.processes import NULL, Sig, Streams
from firstmate.signals import Stop, Stops

# Reports its own pid, then replaces itself with a bash that reports its pid
# again, traps TERM by writing "term", and waits.
_REPORTS_THEN_EXECS = r"""
printf '%s\n' "$$" > "$1/before"
exec bash -c '
  trap "printf term > \"\$1/term\"; exit 143" TERM
  printf "%s\n" "$$" > "$1/after"
  while :; do sleep 0.1; done
' bash "$1"
"""

# Traps TERM by writing "term", says it is ready, and waits.
_TRAPS_TERM = r"""
trap 'printf term > "$1/term"; exit 143' TERM
: > "$1/ready"
while :; do sleep 0.1; done
"""


def _read(path: str) -> str:
    try:
        with open(path) as handle:
            return handle.read().strip()
    except FileNotFoundError:
        return ""


class ShellProcessTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fm-shell-processes.")
        self.stops = Stops()
        self.bash = Bash(BIN, (), "test", {}, dict(os.environ), self.stops)

    def end(self, started: Started) -> None:
        if started.proc.poll() is None:
            self.bash.send(started.pid, Sig.KILL)
            started.proc.wait(timeout=HANG_GUARD)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def test_a_started_child_is_known_by_its_shell_pid_through_an_exec(self):
        started = self.bash.spawn(
            [self.bash.executable, "-c", _REPORTS_THEN_EXECS, "bash", self.dir],
            Streams(NULL, NULL),
        )
        self.addCleanup(self.end, started)
        until(lambda: bool(_read(self.path("after"))), "the child execs")
        reported = (_read(self.path("before")), _read(self.path("after")))
        self.assertEqual(reported, (str(started.pid), str(started.pid)))

    def test_term_sent_by_shell_pid_reaches_the_trap_of_a_child_that_execed(self):
        started = self.bash.spawn(
            [self.bash.executable, "-c", _REPORTS_THEN_EXECS, "bash", self.dir],
            Streams(NULL, NULL),
        )
        self.addCleanup(self.end, started)
        until(lambda: bool(_read(self.path("after"))), "the child execs")
        self.assertTrue(self.bash.alive(started.pid))
        self.assertTrue(self.bash.send(started.pid, Sig.TERM))
        self.assertEqual(started.proc.wait(timeout=HANG_GUARD), 143)
        self.assertEqual(_read(self.path("term")), "term")
        until(lambda: not self.bash.alive(started.pid), "the child is gone")

    def test_an_overdue_foreground_call_is_ended_through_its_term_trap(self):
        stops = Stops(grace=0)
        bash = Bash(BIN, (), "test", {}, dict(os.environ), stops)
        script = self.path("traps-term.sh")
        with open(script, "w") as handle:
            handle.write(_TRAPS_TERM)

        def stop_once_ready():
            until(lambda: os.path.exists(self.path("ready")), "the call traps TERM")
            stops.handle(signal.SIGTERM)

        stopper = threading.Thread(target=stop_once_ready)
        stopper.start()
        self.addCleanup(stopper.join)
        with self.assertRaises(Stop), stops.shielded():
            result = bash.call("source", script, self.dir, stdout=NULL)
        self.assertEqual((result.rc, _read(self.path("term"))), (143, "term"))


if __name__ == "__main__":
    unittest.main()
