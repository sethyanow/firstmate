"""A turn's start, on a fixture home with the real libraries and dispatch script."""

import os
import shutil
import subprocess
import sys
import unittest

from support import BIN, HANG_GUARD, Home, write

from firstmate.host.libs import Engine
from firstmate.host.turn import TurnOutcome


class TurnPostureTest(unittest.TestCase):
    """A turn judges the posture the record gives when it starts, not the one
    the loop accepted its close in."""

    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.contract("enter", "--words", "watch the fleet; merge nothing")
        # An empty queue: an away turn finds nothing to claim and runs no engine.
        write(self.home.at(".wake-queue"), "")

    def contract(self, *args):
        subprocess.run(
            [os.path.join(BIN, "fm-afk-contract.sh"), *args],
            env=self.home.env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

    def handle(self):
        host = self.home.host()
        return host.park.turns.handle("signal: demo.status", Engine("claude", "sonnet"))

    def test_an_away_turn_scopes_its_close_for_the_branch(self):
        self.assertIs(self.handle().outcome, TurnOutcome.NO_CLAIM)

    def test_a_close_accepted_away_is_kept_for_main_once_the_captain_returned(self):
        self.contract("archive")
        self.assertIs(self.handle().outcome, TurnOutcome.KEPT_FOR_MAIN)

    def test_a_turn_prepared_after_the_early_boundary_check_gives_its_wake_back(self):
        harness = os.path.join(self.home.home, "claude")
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash not installed")
        os.symlink(bash, harness)
        engine = os.path.join(self.home.home, "engine")
        write(engine, f'#!{bash}\n: > "$FM_HOME/engine-started"\nexit 3\n')
        os.chmod(engine, 0o700)
        code = (
            "import os, sys\n"
            f"sys.path.insert(0, {os.path.dirname(__file__)!r})\n"
            "from test_park import SetClock\n"
            "from firstmate import files\n"
            "from firstmate.host import assemble\n"
            "from firstmate.host.libs import Engine\n"
            "from firstmate.signals import Stops\n"
            "clock = SetClock()\n"
            f"host = assemble(os.environ, {BIN!r}, Stops(), restart=False, clock=clock)\n"
            "assert host.ctx.libs.append_wake('check', 'boundary', 'check: boundary')\n"
            "assert not host.park.boundary.turn_crosses()\n"
            "clock.seconds = 90\n"
            "result = host.park.turns.handle('check: boundary', Engine('claude', 'sonnet'))\n"
            "print(result.outcome.name)\n"
            "print('prepared', files.nonempty(host.ctx.paths.wake_file))\n"
            "print('turn record', os.path.exists(host.ctx.paths.turn_file))\n"
            "print('queued', host.ctx.libs.rows_queued(('1',)).strip())\n"
        )
        result = subprocess.run(
            [
                harness,
                "-c",
                'printf "%s\\n" "$$" > "$FM_HOME/state/.lock"; "$@"',
                "_",
                sys.executable,
                "-I",
                "-c",
                code,
            ],
            env=self.home.env(
                FM_SUPERVISION_HOST_PARK_SECONDS="100",
                FM_SUPERVISION_HOST_TURN_TIMEOUT="20",
                FM_SUPERVISION_ENGINE_CLAUDE_BIN=engine,
            ),
            capture_output=True,
            text=True,
            timeout=HANG_GUARD,
            check=False,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(result.stdout, "BOUNDARY\nprepared True\nturn record False\nqueued 1\n")
        self.assertFalse(os.path.exists(os.path.join(self.home.home, "engine-started")))


if __name__ == "__main__":
    unittest.main()
