"""Warm compaction while parked, on a fixture home with the real libraries."""

import os
import subprocess
import time
import unittest

from support import BIN, Home, write


def main_key(home: Home) -> str:
    """The main-session key the engine library gives the home's session."""
    return subprocess.run(
        [
            "bash",
            "-c",
            '. "$1/fm-wake-lib.sh"; . "$1/fm-supervision-engine-lib.sh"; '
            'fm_supervision_host_main_key "$2"',
            "fm-test",
            BIN,
            home.state,
        ],
        env=home.env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class EngineCompactionStartTest(unittest.TestCase):
    """An engine compaction that cannot start is skipped, and the park goes on."""

    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        config = os.path.join(self.home.home, "config")
        write(os.path.join(config, "warm-compact"), "idle-seconds=60\n")
        write(os.path.join(config, "supervision-host"), "claude sonnet\n")
        write(self.home.at(".lock"), f"{os.getpid()}\n")
        write(self.home.at(".supervision-host-prompt"), "rendered prompt\n" * 128)
        # A session id longer than the kernel takes in one argument, so the
        # compaction's process cannot start.
        session = "s" * (4 * 1024 * 1024)
        write(
            self.home.at(".supervision-host-engine"),
            f"engine=claude\nmodel=sonnet\nsession={session}\nkey={main_key(self.home)}\n"
            f"turns=1\nconversation_cost=0\nended={int(time.time()) - 600}\n",
        )

    def verdict_files(self):
        return [
            name
            for name in os.listdir(self.home.state)
            if name.startswith(".supervision-host-warm.verdict")
        ]

    def test_a_compaction_that_cannot_start_leaves_nothing_behind(self):
        warm = self.home.host().park.warm
        descriptors = len(os.listdir("/dev/fd"))
        warm.tick(lambda: False)
        self.assertEqual(len(os.listdir("/dev/fd")), descriptors, "a descriptor leaked")
        self.assertEqual(self.verdict_files(), [])


if __name__ == "__main__":
    unittest.main()
