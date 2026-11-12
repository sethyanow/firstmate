"""A turn's start, on a fixture home with the real libraries and dispatch script."""

import os
import subprocess
import unittest

from support import BIN, Home, write

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


if __name__ == "__main__":
    unittest.main()
