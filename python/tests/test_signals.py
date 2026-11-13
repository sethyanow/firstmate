"""Stop signals: the handling itself, and a real process's dispositions."""

import signal
import unittest

from support import run_python

from firstmate.signals import Stop, Stops

_PRELUDE = (
    "import os, signal, subprocess\n"
    "from firstmate.signals import Stop, Stops, start_as_bash\n"
    "STOPS = Stops()\n"
)
# A child reports whether it started with QUIT ignored.
_CHILD_QUIT = (
    "import sys\n"
    "probe = 'import signal; print(signal.getsignal(signal.SIGQUIT) == signal.SIG_IGN)'\n"
    "child = subprocess.run([sys.executable, '-I', '-c', probe], capture_output=True, text=True)\n"
    "print('child QUIT ignored:', child.stdout.strip(), flush=True)\n"
)


class StopHandlingTest(unittest.TestCase):
    def test_a_stop_signal_ends_with_128_plus_its_number(self):
        for sig, status in ((signal.SIGTERM, 143), (signal.SIGHUP, 129), (signal.SIGINT, 130)):
            with self.assertRaises(Stop) as stopped:
                Stops().handle(sig)
            self.assertEqual(stopped.exception.status, status)

    def test_shielded_work_finishes_before_the_first_stop(self):
        stops = Stops()
        finished = []
        with self.assertRaises(Stop) as stopped, stops.shielded():
            with stops.shielded():
                stops.handle(signal.SIGTERM)
                stops.handle(signal.SIGHUP)
            finished.append("inner")
            finished.append("outer")
        self.assertEqual((finished, stopped.exception.status), (["inner", "outer"], 143))

    def test_a_stop_still_ends_shielded_work_that_failed(self):
        stops = Stops()
        with self.assertRaises(Stop), stops.shielded():
            stops.handle(signal.SIGTERM)
            raise OSError("the shielded work failed")

    def test_once_disarmed_for_cleanup_a_pending_stop_no_longer_interrupts(self):
        stops = Stops()
        with self.assertRaises(Stop), stops.shielded():
            stops.handle(signal.SIGTERM)
        stops.disarm()
        with stops.shielded():
            pass

    def test_shielded_work_without_a_signal_is_not_stopped(self):
        with Stops().shielded():
            pass

    def test_a_pending_stop_is_overdue_only_after_its_grace(self):
        self.assertFalse(Stops().overdue())
        stops = Stops(grace=0)
        with self.assertRaises(Stop), stops.shielded():
            stops.handle(signal.SIGTERM)
            self.assertTrue(stops.overdue())
        lasting = Stops(grace=3600)
        with self.assertRaises(Stop), lasting.shielded():
            lasting.handle(signal.SIGTERM)
            self.assertFalse(lasting.overdue())


class ProcessDispositionTest(unittest.TestCase):
    def test_once_disarmed_a_further_stop_signal_ends_the_process_at_once(self):
        status, out = run_python(
            _PRELUDE + "STOPS.install()\n"
            "try:\n"
            "    with STOPS.shielded():\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "except Stop as stop:\n"
            "    print(stop.status, flush=True)\n"
            "STOPS.disarm()\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n"
            "print('survived', flush=True)\n"
        )
        self.assertEqual((status, out), (-signal.SIGTERM, "143\n"))

    def test_a_stop_signal_ignored_at_start_stays_ignored(self):
        status, out = run_python(
            _PRELUDE + "STOPS.install()\n"
            "os.kill(os.getpid(), signal.SIGHUP)\n"
            "STOPS.disarm()\n"
            "os.kill(os.getpid(), signal.SIGHUP)\n"
            "print('ignored', flush=True)\n",
            ignored="HUP",
        )
        self.assertEqual((status, out), (0, "ignored\n"))

    def test_quit_is_ignored_as_bash_ignores_it_while_children_keep_its_default(self):
        status, out = run_python(
            _PRELUDE + "start_as_bash()\nos.kill(os.getpid(), signal.SIGQUIT)\n" + _CHILD_QUIT
        )
        self.assertEqual((status, out), (0, "child QUIT ignored: False\n"))

    def test_quit_ignored_at_start_stays_ignored_for_children(self):
        status, out = run_python(_PRELUDE + "start_as_bash()\n" + _CHILD_QUIT, ignored="QUIT")
        self.assertEqual((status, out), (0, "child QUIT ignored: True\n"))

    def test_an_int_not_ignored_at_start_ends_a_process_that_handles_no_stop_yet(self):
        status, _ = run_python(
            _PRELUDE + "start_as_bash()\nos.kill(os.getpid(), signal.SIGINT)\nprint('survived')\n"
        )
        self.assertEqual(status, -signal.SIGINT)


# The signals Windows CPython's signal module has.
_WINDOWS_SIGNALS = ("SIGABRT", "SIGBREAK", "SIGFPE", "SIGILL", "SIGINT", "SIGSEGV", "SIGTERM")


class WindowsSignalsTest(unittest.TestCase):
    """The host starts where only the Windows signals exist."""

    def test_the_host_imports_and_handles_its_stops_without_unix_only_signals(self):
        code = (
            "import contextlib, io, signal\n"
            "for name in dir(signal):\n"
            "    if name.startswith('SIG') and not name.startswith('SIG_') "
            f"and name not in {_WINDOWS_SIGNALS!r}:\n"
            "        delattr(signal, name)\n"
            "from firstmate.cli import main\n"
            "from firstmate.signals import Stops, start_as_bash\n"
            "start_as_bash()\n"
            "stops = Stops()\n"
            "stops.install()\n"
            "print('TERM', signal.getsignal(signal.SIGTERM) == stops.handle)\n"
            "print('INT', signal.getsignal(signal.SIGINT) == stops.handle)\n"
            "with contextlib.redirect_stdout(io.StringIO()) as help_text:\n"
            "    status = main(['host', '--help'])\n"
            "print('help', status, bool(help_text.getvalue()))\n"
        )
        self.assertEqual(run_python(code), (0, "TERM True\nINT True\nhelp 0 True\n"))


if __name__ == "__main__":
    unittest.main()
