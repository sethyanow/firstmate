"""The bin scripts the host runs, started where the OS cannot run a script itself."""

import os
import shlex
import shutil
import unittest

from support import BIN, Home


class ShellScriptStartTest(unittest.TestCase):
    """Windows does not read a script's shebang, so the host starts its shell
    scripts through bash."""

    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.cleanup)
        self.bin = os.path.join(self.home.home, "bin")
        shutil.copytree(BIN, self.bin, symlinks=True)
        self.ran = os.path.join(self.home.home, "grant-args")
        grant = os.path.join(self.bin, "fm-wake-grant.sh")
        with open(grant, "w") as handle:
            handle.write(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > {shlex.quote(self.ran)}\n')
        # As on a Windows checkout, the OS cannot start the script itself.
        os.chmod(grant, 0o644)

    def test_a_script_without_the_exec_bit_still_runs_with_its_arguments(self):
        ctx = self.home.host(self.bin).ctx
        self.assertEqual(ctx.grant("publish", "gen-1"), 0)
        with open(self.ran) as handle:
            self.assertEqual(handle.read(), "publish\ngen-1\n")


if __name__ == "__main__":
    unittest.main()
