"""The park's boundary, on the clock the host is given."""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from support import BIN, HANG_GUARD, Home, write

# Hold thread startup until the queued pipe stop has reached the main thread.
# This is an OS scheduling boundary, independent of park's source layout.
_QUEUED_STOP = """
import _thread, os, signal, threading
from firstmate.host import park
from firstmate.signals import Stops
delivered = threading.Event()
interrupt, start = _thread.interrupt_main, threading.Thread.start
def confirm_interrupt(signum):
    interrupt(signum)
    delivered.set()
def start_and_deliver(thread):
    start(thread)
    handler = signal.getsignal(signal.SIGTERM)
    if isinstance(getattr(handler, '__self__', None), Stops):
        assert delivered.wait(10), 'queued stop never delivered'
_thread.interrupt_main = confirm_interrupt
threading.Thread.start = start_and_deliver
"""


class SetClock:
    """A park clock the test sets."""

    def __init__(self) -> None:
        self.seconds = 0

    def elapsed(self) -> int:
        return self.seconds

    def restart(self) -> None:
        self.seconds = 0


class ParkBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.clock = SetClock()

    def park(self, **env):
        host = self.home.host(clock=self.clock, FM_SUPERVISION_HOST_PARK_SECONDS="100", **env)
        return host.park

    def test_the_park_ends_at_its_boundary_on_the_clock_it_was_given(self):
        park = self.park()
        self.clock.seconds = 99
        self.assertFalse(park.boundary_reached())
        self.clock.seconds = 100
        self.assertTrue(park.boundary_reached())

    def test_a_model_free_codex_park_starts_its_clock_over_instead_of_ending(self):
        park = self.park(FM_SUPERVISION_HOST_PRIMARY="codex", FM_CODEX_MODEL_FREE_PARK="1")
        self.clock.seconds = 100
        self.assertFalse(park.boundary_reached())
        self.assertEqual(self.clock.elapsed(), 0)


class ParkStartupTest(unittest.TestCase):
    def test_a_stop_queued_before_relay_start_returns_its_status_without_a_traceback(self):
        home = Home()
        self.addCleanup(home.cleanup)
        node = shutil.which("node")
        if node is None:
            self.skipTest("node required for the fixture session owner")
        harness = os.path.join(home.home, "claude.exe" if os.name == "nt" else "claude")
        if os.name == "nt":
            shutil.copyfile(node, harness)
        else:
            os.symlink(node, harness)
        program = os.path.join(home.home, "park.py")
        write(
            program,
            _QUEUED_STOP
            + f"print('park', park(os.environ, {Path(BIN).as_posix()!r}, restart=False))\n",
        )
        launcher = """
const fs = require('fs'), cp = require('child_process');
fs.writeFileSync(process.argv[1], (process.platform === 'win32' ? 'win:' : '') + process.pid);
const child = cp.spawnSync(process.argv[2], ['-I', process.argv[3]], {
    env: {...process.env, CLAUDE_PID: String(process.pid)}, input: '15\\n', encoding: 'utf8'
});
process.stdout.write(child.stdout || '');
process.stderr.write(child.stderr || '');
process.exit(child.status === null ? 1 : child.status);
"""
        result = subprocess.run(
            [harness, "-e", launcher, home.at(".lock"), sys.executable, program],
            env=home.env(FM_HOST_STOP_RELAY="1", CLAUDE_CODE_SESSION_ID="park-startup"),
            capture_output=True,
            text=True,
            timeout=HANG_GUARD,
            check=False,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "park 143\n", ""))
        self.assertFalse(os.path.exists(home.at(".supervision-host")))


if __name__ == "__main__":
    unittest.main()
