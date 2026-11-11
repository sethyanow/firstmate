"""The host's decisions, called directly: no processes, no clock."""

import os
import signal
import subprocess
import sys
import tempfile
import unittest

from firstmate import files
from firstmate.host import latch, records, text
from firstmate.host.settings import numeric_or, paths_from, settings_from
from firstmate.signals import Stop, Stops
from support import HANG_GUARD


class TunablesTest(unittest.TestCase):
    def test_only_a_positive_integer_without_a_leading_zero_replaces_the_default(self):
        for value in ("", None, "0", "007", "-5", "1.5", "12a", " 12", "٣"):
            self.assertEqual(numeric_or(value, 9), 9, value)
        self.assertEqual(numeric_or("12", 9), 12)

    def test_defaults(self):
        settings = settings_from({})
        self.assertEqual((settings.park_seconds, settings.park_limit, settings.turn_timeout, settings.rotate_turns,
                          settings.ready_timeout, settings.poll),
                         (27000, 27000, 1200, 20, 25, 1))

    def test_a_park_at_or_beyond_the_hook_registration_falls_back_to_the_default(self):
        self.assertEqual(settings_from({"FM_SUPERVISION_HOST_PARK_SECONDS": "28800"}).park_seconds, 27000)
        self.assertEqual(settings_from({"FM_SUPERVISION_HOST_PARK_SECONDS": "28799"}).park_seconds, 28799)

    def test_the_turn_limit_is_the_boundary_unless_a_later_one_fits_the_registration(self):
        def limit(seconds, value):
            return settings_from({"FM_SUPERVISION_HOST_PARK_SECONDS": seconds,
                                  "FM_SUPERVISION_HOST_PARK_LIMIT": value}).park_limit
        self.assertEqual(limit("180", "1500"), 1500)
        self.assertEqual(limit("180", "100"), 180)
        self.assertEqual(limit("180", "28800"), 180)

    def test_state_and_config_follow_their_overrides_then_the_home(self):
        paths = paths_from({}, "/repo/bin")
        self.assertEqual((paths.root, paths.home, paths.state, paths.config),
                         ("/repo", "/repo", "/repo/state", "/repo/config"))
        paths = paths_from({"FM_HOME": "/home", "FM_STATE_OVERRIDE": "/s"}, "/repo/bin")
        self.assertEqual((paths.home, paths.state, paths.config), ("/home", "/s", "/home/config"))


class CloseTextTest(unittest.TestCase):
    def test_wake_lines_are_the_close_lines_main_or_the_engine_acts_on(self):
        close = "watcher: started pid=4\nsignal: a.status\nnoise\nheartbeat\nheartbeats\nheartbeat: x\ncheck: y"
        self.assertEqual(text.wake_lines(close), "signal: a.status\nheartbeat\nheartbeat: x\ncheck: y")
        self.assertEqual(text.wake_lines("watcher: attached pid=4"), "")

    def test_the_status_line_is_read_only_once_the_arm_wrote_it_in_full(self):
        self.assertEqual(text.status_line("watcher: started pid=1"), "")
        self.assertEqual(text.status_line("x\nwatcher: started pid=1 (beacon)\n"), "watcher: started pid=1 (beacon)")
        self.assertEqual(text.status_line("watcher: stopped\n"), "")

    def test_a_printed_status_line_leaves_the_first_close_once(self):
        close = "watcher: started pid=1\nsignal: x\nwatcher: started pid=1"
        self.assertEqual(text.without_status(close, "watcher: started pid=1"), "signal: x\nwatcher: started pid=1")

    def test_a_verified_successor_names_its_watcher_and_last_recovery_generation(self):
        line = "watcher: started pid=77 a recovery-generation=1 recovery-generation=g9"
        self.assertEqual(text.verified_watcher("x\n" + line), ("77", "g9"))
        self.assertEqual(text.verified_watcher("watcher: attached pid=8 (beacon fresh)"), ("8", ""))
        self.assertIsNone(text.verified_watcher("watcher: started (no pid)"))

    def test_an_exit_prints_the_close_then_each_non_empty_line(self):
        self.assertEqual(text.compose("signal: x", ["", "supervision-host: why", "a\nb"]),
                         "signal: x\nsupervision-host: why\na\nb")
        self.assertEqual(text.compose("", ["supervision-host: why"]), "supervision-host: why")
        self.assertEqual(text.compose("", ["", ""]), "")

    def test_a_failed_turn_names_its_first_failure(self):
        def why(rc=0, timed_out=False, usage="error=0 cost=1", receipts=1, unacked="4 5"):
            return text.turn_failure(rc, timed_out, usage, receipts, unacked, 1200, "claude")
        self.assertEqual(why(rc=124, timed_out=True), "the engine turn hit its 1200s bound")
        self.assertEqual(why(rc=127), "the claude engine could not run")
        self.assertEqual(why(rc=3), "the engine turn failed (exit 3)")
        self.assertEqual(why(usage="error=1 cost=1"), "the engine turn ended with an error or an incomplete result")
        self.assertEqual(why(usage=""), "the engine turn ended with an error or an incomplete result")
        self.assertEqual(why(receipts=0), "the engine turn recorded no outcome for its wake")
        self.assertEqual(why(), "the engine turn left its granted wake rows 4 5 unacknowledged")


class UnacknowledgedRowsTest(unittest.TestCase):
    def test_the_rows_the_queue_still_holds_are_named(self):
        self.assertEqual(text.unacknowledged("4 5 6", "4\n6\n"), "4 6")
        self.assertEqual(text.unacknowledged("4 5 6", ""), "")

    def test_every_granted_row_is_named_when_the_queue_cannot_be_read(self):
        self.assertEqual(text.unacknowledged("4 5 6", None), "4 5 6")


class OutcomeRowsTest(unittest.TestCase):
    ROWS = ('{"seq":2,"task":"demo","verdict":"routine","summary":"done\\r","silent":false}\n'
            '{"seq":3,"task":"demo","verdict":"routine","summary":"quiet","silent":true}\n'
            '{"seq":4,"task":"beta","verdict":"captain","summary":"merge?"}')

    def test_silent_outcomes_are_left_out(self):
        self.assertEqual(text.visible_seqs(self.ROWS), "2, 4")
        self.assertEqual(text.outcome_lines(self.ROWS),
                         "supervision-host: outcome 2 for demo [routine]: done\n"
                         "supervision-host: outcome 4 for beta [captain]: merge?")

    def test_no_rows_name_no_outcome(self):
        self.assertEqual(text.visible_seqs(""), "")

    def test_a_malformed_row_is_a_lookup_failure_not_silence(self):
        for rows in ("not-json", '{"seq":1}\n7', '{"seq":1'):
            with self.assertRaises(ValueError, msg=rows):
                text.visible_seqs(rows)


class LatchTest(unittest.TestCase):
    def after(self, state, error, reports=0, now=1000):
        return latch.after_turn(state, error, reports, now, 300, 3600)

    def test_one_engine_error_does_not_trip(self):
        state, note, logged = self.after(latch.Latch(), True)
        self.assertEqual((state, note, logged), (latch.Latch(errors=1), "", []))

    def test_the_second_consecutive_error_trips_with_one_note(self):
        state, note, logged = self.after(latch.Latch(errors=1), True)
        self.assertEqual(state, latch.Latch(errors=2, cooldown=300, retry=1300))
        self.assertTrue(note.startswith("supervision-host: the supervision session is paused after repeated engine "
                                        "errors; every wake reaches you for the next 5 minutes"))
        self.assertEqual(logged, ["latch\terrors=2\tcooldown=300s"])

    def test_a_failed_probe_doubles_the_cooldown_up_to_its_cap_without_a_note(self):
        state, note, _ = self.after(latch.Latch(errors=2, cooldown=300, retry=900), True)
        self.assertEqual((state.cooldown, state.retry, note), (600, 1600, ""))
        state, _, _ = self.after(latch.Latch(errors=5, cooldown=2400, retry=900), True)
        self.assertEqual(state.cooldown, 3600)

    def test_a_report_without_an_engine_error_clears_it(self):
        self.assertEqual(self.after(latch.Latch(errors=2, cooldown=300, retry=9), False, reports=1),
                         (latch.Latch(), "", ["recovered\tafter a successful probe"]))
        self.assertEqual(self.after(latch.Latch(errors=1), False, reports=1), (latch.Latch(), "", []))

    def test_a_complete_turn_without_a_report_neither_counts_nor_clears(self):
        self.assertEqual(self.after(latch.Latch(errors=1), False)[0], latch.Latch(errors=1))
        self.assertEqual(self.after(latch.Latch(errors=2, cooldown=300, retry=1000), False)[0],
                         latch.Latch(errors=2, cooldown=300, retry=1300))
        self.assertEqual(self.after(latch.Latch(errors=2, cooldown=300, retry=1001), False)[0],
                         latch.Latch(errors=2, cooldown=300, retry=1001))

    def test_the_latch_belongs_to_its_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "health")
            latch.save(path, "k1", latch.Latch(errors=2, cooldown=300, retry=50))
            self.assertEqual(latch.load(path, "k1"), latch.Latch(errors=2, cooldown=300, retry=50))
            self.assertEqual(latch.load(path, "k2"), latch.Latch())
            self.assertEqual(latch.load(path, None), latch.Latch())
            latch.save(path, None, latch.Latch(errors=9))
            self.assertEqual(files.field(path, "errors"), "2")


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "record")

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_round_trip_and_forget_drops_only_their_pid(self):
        records.append(self.path, "host", 10, "id-h")
        records.append(self.path, "arm", 11, "id-a")
        records.append(self.path, "turn", 12, "id\tt")
        records.forget(self.path, 11)
        self.assertEqual(records.rows(self.path), [["host", "10", "id-h"], ["turn", "12", "id\tt"]])
        self.assertEqual(records.host_pid(self.path), "10")

    def test_refresh_rewrites_only_a_changed_identity(self):
        records.append(self.path, "arm", 11, "before-exec")
        records.append(self.path, "arm", 12, "other")
        records.refresh(self.path, 11, "after-exec")
        self.assertEqual(records.rows(self.path), [["arm", "11", "after-exec"], ["arm", "12", "other"]])
        records.refresh(self.path, 12, "")
        self.assertEqual(records.rows(self.path)[1], ["arm", "12", "other"])

    def test_an_unterminated_last_row_names_no_process(self):
        with open(self.path, "w") as handle:
            handle.write("arm\t1\tid\narm\t2\tpart")
        self.assertEqual(records.rows(self.path), [["arm", "1", "id"]])

    def test_the_ledger_stays_bounded_and_keeps_its_newest_rows_in_order(self):
        written = [f"5\trow {n}" for n in range(5000)]
        for row in written:
            records.log(self.path, row.split("\t", 1)[1], 5)
        kept = files.lines(files.read_text(self.path))
        self.assertLess(len(kept), len(written))
        self.assertEqual(kept, written[len(written) - len(kept):])


class StopSignalTest(unittest.TestCase):
    def test_a_stop_signal_ends_with_128_plus_its_number(self):
        for sig, status in ((signal.SIGTERM, 143), (signal.SIGHUP, 129), (signal.SIGINT, 130)):
            with self.assertRaises(Stop) as stopped:
                Stops().handle(sig)
            self.assertEqual(stopped.exception.status, status)

    def test_shielded_work_finishes_before_the_stop(self):
        stops = Stops()
        finished = []
        with self.assertRaises(Stop) as stopped, stops.shielded():
            with stops.shielded():
                stops.handle(signal.SIGTERM)
                stops.handle(signal.SIGHUP)
            finished.append("inner")
            finished.append("outer")
        self.assertEqual((finished, stopped.exception.status), (["inner", "outer"], 143))

    def test_once_disarmed_for_cleanup_a_pending_stop_no_longer_interrupts(self):
        stops = Stops()
        with self.assertRaises(Stop), stops.shielded():
            stops.handle(signal.SIGTERM)
        stops.disarm()
        with stops.shielded():
            pass

    def run_stops(self, code, ignored=""):
        """Run `code` with STOPS in a fresh process started with the `ignored`
        signals ignored; (status, output)."""
        proc = subprocess.run(["bash", "-c", f"trap '' {ignored}; exec \"$0\" -I -c \"$1\"" if ignored
                               else 'exec "$0" -I -c "$1"', sys.executable,
                               "import os, signal\nfrom firstmate.signals import STOPS, Stop\n" + code],
                              capture_output=True, text=True, timeout=HANG_GUARD, check=False)
        return proc.returncode, proc.stdout

    def test_once_disarmed_a_further_stop_signal_ends_the_process_at_once(self):
        status, out = self.run_stops(
            "STOPS.install()\n"
            "try:\n"
            "    with STOPS.shielded():\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "except Stop as stop:\n"
            "    print(stop.status, flush=True)\n"
            "STOPS.disarm()\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n"
            "print('survived', flush=True)\n")
        self.assertEqual((status, out), (-signal.SIGTERM, "143\n"))

    def test_a_stop_signal_ignored_at_start_stays_ignored(self):
        status, out = self.run_stops(
            "STOPS.install()\n"
            "os.kill(os.getpid(), signal.SIGHUP)\n"
            "STOPS.disarm()\n"
            "os.kill(os.getpid(), signal.SIGHUP)\n"
            "print('ignored', flush=True)\n", ignored="HUP")
        self.assertEqual((status, out), (0, "ignored\n"))

    def test_shielded_work_without_a_signal_is_not_stopped(self):
        stops = Stops()
        with stops.shielded():
            pass
        self.assertIsNone(stops.pending)


if __name__ == "__main__":
    unittest.main()
