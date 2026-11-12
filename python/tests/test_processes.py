"""Foreground commands, library calls, and output under a stop signal, with real processes."""

import os
import signal
import subprocess
import unittest

from support import BIN, HANG_GUARD, Home, run_python, until

from firstmate import files
from firstmate.bashlib import Bash
from firstmate.host.context import Output
from firstmate.processes import Command, pid_alive, run
from firstmate.signals import Stop, Stops


class ObservedStops(Stops):
    """Stop handling that marks the moment it took a signal, so a child can wait
    on that condition instead of a clock."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self.marker = marker

    def handle(self, signum, _frame=None):
        files.write(self.marker, "")
        super().handle(signum, _frame)


def library(env, stops):
    return Bash(BIN, ("fm-wake-lib.sh",), "fm-test", {}, env, stops)


class ChildStatusTest(unittest.TestCase):
    def test_a_command_a_signal_ends_reports_128_plus_the_signal(self):
        done = run(Command(["bash", "-c", "kill -TERM $$"], os.environ), Stops())
        self.assertEqual((done.rc, done.out), (143, ""))

    def test_a_command_that_cannot_start_fails_as_a_shell_reports_it(self):
        self.assertEqual(run(Command(["/nonexistent/command"], os.environ), Stops()).rc, 127)

    def test_a_library_call_a_signal_ends_reports_128_plus_the_signal(self):
        self.assertEqual(library(os.environ, Stops()).call("eval", "kill -KILL $$").rc, 137)

    def test_a_library_call_that_cannot_start_fails_as_a_missing_command(self):
        home = Home()
        self.addCleanup(home.cleanup)
        bash = library({"PATH": os.path.join(home.home, "no-bin")}, Stops())
        result = bash.call("true", out_vars=("FM_VALUE",))
        self.assertEqual((result.rc, dict(result.values)), (127, {"FM_VALUE": ""}))

    def test_a_library_call_returns_its_output_and_variables(self):
        result = library(os.environ, Stops()).call(
            "eval", "printf 'out\\n\\n'; FM_A=1; FM_B='two words'", out_vars=("FM_A", "FM_B")
        )
        self.assertEqual(
            (result.rc, result.text, dict(result.values)),
            (0, "out", {"FM_A": "1", "FM_B": "two words"}),
        )


class ForegroundWorkTest(unittest.TestCase):
    """A stop signal that lands while foreground work runs takes effect once that
    work is done. The parent is this test's process, standing in for the host."""

    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        saved = {
            sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)
        }
        self.addCleanup(lambda: [signal.signal(sig, handler) for sig, handler in saved.items()])
        marker = os.path.join(self.home.home, "stop-seen")
        self.stops = ObservedStops(marker)
        self.stops.install()
        self.addCleanup(self.stops.disarm)
        self.done = os.path.join(self.home.home, "done")
        # The child finishes its work only after the parent took the signal.
        self.script = (
            f'kill -TERM "$PPID"; '
            f"i=0; while [ ! -e {marker} ] && [ $i -lt {HANG_GUARD * 100} ]; "
            "do sleep 0.01; i=$((i+1)); done; "
            f"printf done > {self.done}"
        )

    def test_a_library_call_finishes_before_the_stop(self):
        with self.assertRaises(Stop) as stopped:
            library(os.environ, self.stops).call("eval", self.script)
        self.assertEqual((stopped.exception.status, files.read_text(self.done)), (143, "done"))

    def test_a_command_finishes_before_the_stop(self):
        with self.assertRaises(Stop) as stopped:
            run(Command(["bash", "-c", self.script], os.environ), self.stops)
        self.assertEqual((stopped.exception.status, files.read_text(self.done)), (143, "done"))


class BoundedStopTest(unittest.TestCase):
    """A TERM always stops the process: a library call still waiting on a live,
    wedged lock holder once the stop's grace passes is ended."""

    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.lock = self.home.at(".wake-queue.lock")
        ready = os.path.join(self.home.home, "holder-ready")
        self.holder = subprocess.Popen(
            [
                "bash",
                "-c",
                '. "$1"; fm_lock_acquire_wait "$2" && : > "$3" && read -r _',
                "_",
                os.path.join(BIN, "fm-wake-lib.sh"),
                self.lock,
                ready,
            ],
            stdin=subprocess.PIPE,
            env={**os.environ, "FM_HOME": self.home.home},
        )
        self.addCleanup(self.release_holder)
        until(lambda: os.path.exists(ready), "the lock holder holds the queue lock")

    def release_holder(self):
        if self.holder.stdin:
            self.holder.stdin.close()
        self.holder.wait(timeout=HANG_GUARD)

    def test_a_call_waiting_on_a_wedged_lock_holder_does_not_hold_off_a_stop(self):
        code = (
            "import os, time\n"
            "from firstmate.bashlib import Bash\n"
            "from firstmate.signals import Stop, Stops\n"
            "stops = Stops(grace=0.5)\n"
            "stops.install()\n"
            f"bash = Bash({BIN!r}, ('fm-wake-lib.sh',), 'fm-test', {{}}, "
            f"{{**os.environ, 'FM_HOME': {self.home.home!r}}}, stops)\n"
            "try:\n"
            f"    bash.call('eval', 'printf %s $$ > {self.home.home}/call-pid; "
            f"kill -TERM $PPID; fm_lock_acquire_wait {self.lock}')\n"
            "except Stop as stop:\n"
            "    print('stopped', stop.status, flush=True)\n"
        )
        status, out = run_python(code)
        self.assertEqual((status, out), (0, "stopped 143\n"))
        call = int(files.read_text(os.path.join(self.home.home, "call-pid")) or "0")
        self.assertFalse(pid_alive(call), "the wedged call outlived the stop")
        self.assertEqual(files.first_line(os.path.join(self.lock, "pid")), str(self.holder.pid))


class OutputTest(unittest.TestCase):
    def test_an_owner_that_closed_the_output_stops_the_host_with_141(self):
        read_end, write_end = os.pipe()
        os.close(read_end)
        self.addCleanup(os.close, write_end)
        with self.assertRaises(Stop) as stopped:
            Output(Stops(), write_end).write("watcher: started pid=1\n")
        self.assertEqual(stopped.exception.status, 141)

    def test_output_reaches_the_owner_in_one_piece(self):
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        Output(Stops(), write_end).write("signal: x\nsupervision-host: why\n")
        os.close(write_end)
        self.assertEqual(os.read(read_end, 4096), b"signal: x\nsupervision-host: why\n")


if __name__ == "__main__":
    unittest.main()
