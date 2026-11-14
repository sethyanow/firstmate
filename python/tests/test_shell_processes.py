"""Processes as the bash libraries know them: by the pid `$$` reports, which on
native Windows is a Git Bash pid, not the Windows one."""

import dataclasses
import os
import signal
import tempfile
import threading
import unittest
from pathlib import Path

from support import BIN, HANG_GUARD, Home, run_python, until

from firstmate.bashlib import Bash, Started
from firstmate.host import records
from firstmate.host.lifecycle import activate
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

    def test_a_child_that_reports_its_pid_and_exits_between_probes_still_starts(self):
        code = f"""
import os, shutil, subprocess
from firstmate import bashlib
from firstmate.processes import CAPTURE, Streams
from firstmate.signals import Stops
real_bash = shutil.which('bash')
class ExitBeforePoll(subprocess.Popen):
    def __init__(self, args, **options):
        super().__init__([real_bash, '-c', 'read -r _; exec "$@"', 'gate', *args], **options)
    def poll(self):
        if self.returncode is None:
            self.stdin.write(b'go\\n')
            self.stdin.flush()
            self.wait(timeout=60)
        return super().poll()
subprocess.Popen = ExitBeforePoll
bashlib.WINDOWS = True
bash = bashlib.Bash({Path(BIN).as_posix()!r}, (), 'test', {{}}, dict(os.environ), Stops())
started = bash.spawn([real_bash, '-c', 'printf "%s\\n" "$$"'],
                     Streams(stdin=subprocess.PIPE, stdout=CAPTURE))
with started.proc.stdin, started.proc.stdout:
    print('reported pid matches', started.pid == int(started.proc.stdout.read()))
print('exit', started.proc.wait(timeout=60))
"""
        self.assertEqual(run_python(code), (0, "reported pid matches True\nexit 0\n"))

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


class SignalCommandFailureTest(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.reapers = []
        self.host = self.home.host()
        self.bash = self.host.ctx.libs.bash
        self.predecessor = self.bash.spawn(
            [self.bash.executable, "-c", _TRAPS_TERM, "bash", self.home.home],
            Streams(NULL, NULL),
        )
        self.pid = self.predecessor.pid
        self.addCleanup(self.end_predecessor)
        until(lambda: os.path.isfile(os.path.join(self.home.home, "ready")), "TERM trap ready")
        self.identity = self.host.ctx.libs.identity_of(self.pid)
        self.assertTrue(self.identity, "the predecessor must have a readable identity")

    def end_predecessor(self):
        if self.predecessor.proc.poll() is None:
            self.bash.send(self.pid, Sig.KILL)
        self.predecessor.proc.wait(timeout=HANG_GUARD)
        for reaper in self.reapers:
            reaper.join(HANG_GUARD)

    def reaper(self):
        reaper = threading.Thread(target=self.predecessor.proc.wait)
        reaper.start()
        self.reapers.append(reaper)

    def test_a_failed_term_is_retried_before_the_predecessors_host_row_is_replaced(self):
        refused = []
        rows_at_retry = []
        record = self.host.ctx.paths.host_record

        class RefusesFirstTerm(Bash):
            def send(self, pid, sig):
                if sig is Sig.TERM and not refused:
                    refused.append(pid)
                    return False
                rows_at_retry.extend((row.role, row.pid) for row in records.rows(record))
                return super().send(pid, sig)

        bash = RefusesFirstTerm(
            self.bash.script_dir,
            self.bash.libraries,
            self.bash.arg0,
            self.bash.shell_globals,
            self.bash.env,
            self.bash.stops,
        )
        libs = dataclasses.replace(self.host.ctx.libs, bash=bash)
        ctx = dataclasses.replace(self.host.ctx, libs=libs)
        procs = dataclasses.replace(self.host.procs, libs=libs)
        self.host.procs.record("host", self.pid)
        self.reaper()

        self.assertIsNotNone(activate(ctx, procs))

        self.assertEqual(refused, [self.pid])
        self.assertEqual(rows_at_retry, [("host", self.pid)])
        self.assertFalse(self.bash.alive(self.pid))
        self.assertEqual(_read(os.path.join(self.home.home, "term")), "term")
        self.assertEqual(
            [(row.role, row.pid) for row in records.rows(ctx.paths.host_record)],
            [("host", ctx.facts.pid)],
        )

    def test_a_failed_term_still_escalates_to_kill_when_the_deadline_expires(self):
        class RefusesTerm(Bash):
            def send(self, pid, sig):
                return False if sig is Sig.TERM else super().send(pid, sig)

        bash = RefusesTerm(
            self.bash.script_dir,
            self.bash.libraries,
            self.bash.arg0,
            self.bash.shell_globals,
            self.bash.env,
            self.bash.stops,
        )
        procs = dataclasses.replace(
            self.host.procs, libs=dataclasses.replace(self.host.ctx.libs, bash=bash)
        )
        procs.stop_recorded(self.pid, self.identity, 0)
        self.predecessor.proc.wait(timeout=HANG_GUARD)
        self.assertFalse(self.bash.alive(self.pid))
        self.assertFalse(os.path.exists(os.path.join(self.home.home, "term")))


if __name__ == "__main__":
    unittest.main()
