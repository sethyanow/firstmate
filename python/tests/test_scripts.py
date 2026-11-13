"""The bin scripts the host runs, started where the OS cannot run a script itself."""

import os
import shlex
import shutil
import unittest

from support import BIN, Home, run_python


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


class WindowsBashSearchTest(unittest.TestCase):
    """Windows searches its system folder, where WSL's bash.exe lives, before
    PATH. The host runs the bash on its own PATH for library calls and scripts."""

    def test_the_bash_on_path_runs_ahead_of_the_system_folder(self):
        code = (
            "import os, shutil, subprocess, tempfile\n"
            f"BIN = {BIN!r}\n"
            "real_bash = shutil.which('bash')\n"
            "root = tempfile.mkdtemp()\n"
            "ran = os.path.join(root, 'ran')\n"
            "def stub(folder, body):\n"
            "    os.makedirs(folder)\n"
            "    path = os.path.join(folder, 'bash')\n"
            "    with open(path, 'w') as handle:\n"
            "        handle.write('#!/bin/sh\\n' + body)\n"
            "    os.chmod(path, 0o755)\n"
            "system = os.path.join(root, 'System32')\n"
            "stub(system, f'echo wsl >> {ran}\\nexit 1\\n')\n"
            "git = f'echo git >> {ran}\\nexec {real_bash} \"$@\"\\n'\n"
            "stub(os.path.join(root, 'git'), git)\n"
            "class WindowsPopen(subprocess.Popen):\n"
            "    def __init__(self, args, *rest, **options):\n"
            "        found = os.path.join(system, args[0])\n"
            "        if os.sep not in args[0] and os.path.exists(found):\n"
            "            args = [found, *args[1:]]\n"
            "        super().__init__(args, *rest, **options)\n"
            "subprocess.Popen = WindowsPopen\n"
            "home = os.path.join(root, 'home')\n"
            "os.makedirs(os.path.join(home, 'state'))\n"
            "os.makedirs(os.path.join(home, 'config'))\n"
            "path = os.path.join(root, 'git') + os.pathsep + os.environ['PATH']\n"
            "env = dict(os.environ, FM_HOME=home, FM_SUPERVISION_HOST_PRIMARY='claude',\n"
            "           FM_GUARD_GRACE='300', PATH=path)\n"
            "from firstmate.host import assemble\n"
            "from firstmate.host.context import MonotonicClock\n"
            "from firstmate.signals import Stops\n"
            "def bashes():\n"
            "    with open(ran) as handle:\n"
            "        found = sorted(set(handle.read().split()))\n"
            "    open(ran, 'w').close()\n"
            "    return found\n"
            "ctx = assemble(env, BIN, Stops(), restart=False, clock=MonotonicClock()).ctx\n"
            "print('assembly', bashes())\n"
            "ctx.libs.bash.call('true')\n"
            "print('call', bashes())\n"
            "ctx.run(ctx.script('fm-afk-contract.sh', 'mode'))\n"
            "print('script', bashes())\n"
        )
        expected = "assembly ['git']\ncall ['git']\nscript ['git']\n"
        self.assertEqual(run_python(code), (0, expected))


if __name__ == "__main__":
    unittest.main()
