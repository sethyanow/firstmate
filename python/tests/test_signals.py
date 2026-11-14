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


# Relays the bytes of $RELAYED to a stop-handling process, then waits in short
# sleeps, as the host's park does, until the relayed stop ends it.
_RELAYED = (
    "import sys, time\n"
    "read_end, write_end = os.pipe()\n"
    "STOPS.install()\n"
    "STOPS.relay_from(os.fdopen(read_end, 'rb'))\n"
    "try:\n"
    "    with STOPS.shielded():\n"
    "        os.write(write_end, RELAYED)\n"
    "        if CLOSE:\n"
    "            os.close(write_end)\n"
    "        print('shielded work finished', flush=True)\n"
    "    for _ in range(600):\n"
    "        time.sleep(0.1)\n"
    "except Stop as stop:\n"
    "    print(stop.status, flush=True)\n"
)

# Force delivery before the publishing pipe operation returns, using the real
# reader and main-thread interrupt. The fixture must already be protected.
_IMMEDIATE_RELAY = """
import _thread, threading
delivered = threading.Event()
interrupt = _thread.interrupt_main
write, close = os.write, os.close
def interrupt_and_confirm(signum):
    interrupt(signum)
    delivered.set()
def publish(fd, data):
    result = write(fd, data)
    if data and not delivered.is_set():
        assert delivered.wait(10), 'relay never delivered'
    return result
def close_and_confirm(fd):
    close(fd)
    assert delivered.wait(10), 'EOF never delivered'
_thread.interrupt_main = interrupt_and_confirm
os.write, os.close = publish, close_and_confirm
"""


class RelayedStopTest(unittest.TestCase):
    """A process its owner cannot signal takes its stop signals as numbers on a
    stream, and the stream's end as TERM."""

    def test_a_relayed_signal_number_stops_the_process_after_shielded_work(self):
        status, out = run_python(
            _PRELUDE + _IMMEDIATE_RELAY + "RELAYED, CLOSE = b'1\\n', False\n" + _RELAYED
        )
        self.assertEqual((status, out), (0, "shielded work finished\n129\n"))

    def test_the_end_of_the_stream_stops_the_process_as_term(self):
        status, out = run_python(
            _PRELUDE + _IMMEDIATE_RELAY + "RELAYED, CLOSE = b'', True\n" + _RELAYED
        )
        self.assertEqual((status, out), (0, "shielded work finished\n143\n"))

    def test_once_disarmed_a_further_relayed_stop_ends_the_process_at_once(self):
        status, out = run_python(
            _PRELUDE + _IMMEDIATE_RELAY + "import time\n"
            "read_end, write_end = os.pipe()\n"
            "STOPS.install()\n"
            "STOPS.relay_from(os.fdopen(read_end, 'rb'))\n"
            "try:\n"
            "    os.write(write_end, b'15\\n')\n"
            "    for _ in range(600):\n"
            "        time.sleep(0.1)\n"
            "except Stop as stop:\n"
            "    print(stop.status, flush=True)\n"
            "STOPS.disarm()\n"
            "os.write(write_end, b'1\\n')\n"
            "for _ in range(600):\n"
            "    time.sleep(0.1)\n"
            "print('survived', flush=True)\n"
        )
        self.assertEqual((status, out), (129, "143\n"))

    def test_a_stop_still_receiving_its_timestamp_keeps_the_foreground_childs_grace(self):
        code = (
            _PRELUDE
            + """
import _thread, sys, tempfile, threading, time
from firstmate.processes import finish
arriving, resume, checked, delivered = (threading.Event() for _ in range(4))
monotonic, interrupt = time.monotonic, _thread.interrupt_main
def clock():
    if threading.current_thread() is not threading.main_thread() and not arriving.is_set():
        arriving.set()
        assert resume.wait(10), 'arrival was never released'
    return monotonic()
def confirm_interrupt(signum):
    interrupt(signum)
    delivered.set()
class ObservedStops(Stops):
    def overdue(self):
        result = super().overdue()
        checked.set()
        return result
time.monotonic, _thread.interrupt_main = clock, confirm_interrupt
STOPS = ObservedStops(grace=5)
STOPS.install()
read_end, write_end = os.pipe()
STOPS.relay_from(os.fdopen(read_end, 'rb'))
terminated = []
def terminate(proc):
    terminated.append(True)
    proc.terminate()
with tempfile.TemporaryDirectory() as home:
    release = os.path.join(home, 'release')
    child = subprocess.Popen([sys.executable, '-I', '-c',
        'import os, sys, time\\n'
        'while not os.path.exists(sys.argv[1]): time.sleep(0.01)\\n'
        'print("finished")', release], stdout=subprocess.PIPE)
    def release_child():
        assert checked.wait(10), 'foreground grace was never checked'
        with open(release, 'w'):
            pass
    releaser = threading.Thread(target=release_child)
    releaser.start()
    try:
        with STOPS.shielded():
            try:
                os.write(write_end, b'15\\n')
                assert arriving.wait(10), 'relay never received the stop'
                output = finish(child, STOPS, terminate=terminate)
                print('finished', output is not None and output.splitlines() == [b'finished'])
                print('terminated', bool(terminated))
            finally:
                resume.set()
                assert delivered.wait(10), 'relay never delivered the stop'
    except Stop as stopped:
        print(stopped.status)
    releaser.join(10)
"""
        )
        self.assertEqual(run_python(code), (0, "finished True\nterminated False\n143\n"))


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
