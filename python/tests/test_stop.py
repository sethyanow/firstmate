"""Each of the host's stop and cleanup steps, called directly on a fixture home."""

import os
import signal
import subprocess
import unittest

from firstmate import files
from firstmate.host import records
from firstmate.host.park import Child, pid_alive
from firstmate.signals import STOPS, Stop
from support import BIN, Home


def write(path, content):
    with open(path, "w") as handle:
        handle.write(content)


class StopStepsTest(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.host = self.home.host()
        self.children = []

    def tearDown(self):
        for proc in self.children:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdin:
                proc.stdin.close()
        self.home.cleanup()

    def child(self, *argv):
        """A process this host started, as its arms and engine turns are."""
        proc = subprocess.Popen(list(argv) or ["bash", "-c", "trap 'exit 42' TERM; read -r _"], stdin=subprocess.PIPE)
        self.children.append(proc)
        return proc

    def identity(self, pid):
        return self.host.identity_of(pid)

    def lease(self, actor, task):
        env = self.home.env(FM_SUPERVISION_ACTOR=actor)
        return subprocess.run([os.path.join(BIN, "fm-lease.sh"), "claim", task], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, check=False)

    def lease_holder(self, task):
        return subprocess.run([os.path.join(BIN, "fm-lease.sh"), "check", task], env=self.home.env(),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False).stdout

    def test_a_host_that_is_constructed_but_not_parked_touches_nothing(self):
        self.assertEqual(os.listdir(self.home.state), [])

    def test_a_recorded_process_is_stopped_through_its_record_and_forgetting_keeps_the_rest(self):
        first = self.home.blocker()
        second = self.home.blocker()
        self.host.record_process("arm", first.pid)
        self.host.record_process("turn", second.pid)
        role, pid, identity = records.rows(self.host.paths.host_record)[0]
        self.assertEqual((role, pid), ("arm", first.pid))
        self.host.stop_recorded(pid, identity, 1)
        first.wait_gone()
        self.assertTrue(first.saw_term)
        self.host.forget_process(first.pid)
        self.assertEqual([row[:2] for row in records.rows(self.host.paths.host_record)], [["turn", second.pid]])

    def test_the_host_record_is_ours_only_while_its_host_row_names_this_host(self):
        record = self.host.paths.host_record
        self.assertFalse(self.host.host_record_is_ours())
        write(record, f"host\t{self.host.pid}\tid\n")
        self.assertTrue(self.host.host_record_is_ours())
        write(record, f"arm\t{self.host.pid}\tid\nhost\t1\tother\n")
        self.assertFalse(self.host.host_record_is_ours())

    def test_stop_recorded_leaves_a_mismatched_or_unidentified_process_alone(self):
        blocker = self.home.blocker()
        self.host.stop_recorded(blocker.pid, "a different identity", 1)
        self.host.stop_recorded(blocker.pid, "", 1)
        blocker.release()
        blocker.wait_gone()
        self.assertEqual((blocker.saw_term, blocker.finished_normally), (False, True))

    def test_stop_recorded_kills_a_matching_process_that_ignores_term(self):
        blocker = self.home.blocker(trap="")
        self.host.stop_recorded(blocker.pid, self.identity(blocker.pid), 0)
        blocker.wait_gone()
        self.assertEqual((blocker.saw_term, blocker.finished_normally), (False, False))

    def test_stop_engine_turn_signals_the_recorded_engine(self):
        engine = self.home.blocker()
        write(self.host.paths.engine_pid_file, f"{engine.pid}\t{self.identity(engine.pid)}\n")
        self.host.stop_engine_turn()
        engine.wait_gone()
        self.assertTrue(engine.saw_term)

    def test_stop_engine_turn_leaves_a_reused_engine_pid_alone(self):
        engine = self.home.blocker()
        write(self.host.paths.engine_pid_file, f"{engine.pid}\tforeign\n")
        self.host.stop_engine_turn()
        engine.release()
        engine.wait_gone()
        self.assertEqual((engine.saw_term, engine.finished_normally), (False, True))

    def test_stop_engine_turn_waits_for_the_turn_to_reap_its_engine(self):
        engine = self.home.blocker()
        write(self.host.paths.engine_pid_file, f"{engine.pid}\t{self.identity(engine.pid)}\n")
        turn = self.child("bash", "-c", 'trap "" TERM; while kill -0 "$1" 2>/dev/null; do sleep 0.05; done; exit 7',
                          "_", engine.pid)
        self.host.engine_proc = turn
        self.host.stop_engine_turn()
        self.assertEqual(turn.returncode, 7)
        self.assertTrue(engine.saw_term)

    def test_releasing_branch_leases_keeps_main_leases(self):
        self.assertEqual(self.lease("branch", "demo").returncode, 0)
        self.assertEqual(self.lease("main", "other").returncode, 0)
        self.host.release_branch_leases()
        self.assertEqual(self.lease_holder("demo"), "")
        self.assertTrue(self.lease_holder("other").startswith("main "))

    def test_retiring_an_arm_stops_it_and_drops_its_row_and_output(self):
        out = self.home.at(".supervision-host-arm.fixture")
        write(out, "watcher: started pid=1\n")
        arm = Child(self.child(), out)
        self.host.record_process("arm", arm.pid)
        self.host.retire_arm(arm)
        self.assertEqual(arm.proc.returncode, 42)
        self.assertEqual(records.rows(self.host.paths.host_record), [])
        self.assertFalse(os.path.exists(out))

    def test_cleanup_stops_its_processes_releases_its_grant_and_leases_and_keeps_the_status(self):
        host, paths = self.host, self.host.paths
        write(paths.host_record, f"host\t{host.pid}\t{self.identity(host.pid)}\n")
        subprocess.run([os.path.join(BIN, "fm-wake-grant.sh"), "activate", host.pid, host.gen], env=self.home.env(),
                       check=True, stdout=subprocess.DEVNULL)
        host.grant_active = True
        self.assertEqual(self.lease("branch", "demo").returncode, 0)
        arm_out = self.home.at(".supervision-host-arm.fixture")
        write(arm_out, "")
        host.arm = Child(self.child(), arm_out)
        successor_out = self.home.at(".supervision-host-arm.successor")
        write(successor_out, "")
        host.successor = Child(self.child(), successor_out)
        engine = self.home.blocker()
        write(paths.engine_pid_file, f"{engine.pid}\t{self.identity(engine.pid)}\n")
        host.engine_proc = self.child("bash", "-c", 'trap "" TERM; while kill -0 "$1" 2>/dev/null; do sleep 0.05; done',
                                      "_", engine.pid)
        host.engine_running = True
        host.turn_result = self.home.at(".supervision-host-result.fixture")
        host.turn_errors = self.home.at(".supervision-host-errors.fixture")
        for path in (paths.turn_file, host.turn_result, host.turn_errors):
            write(path, "")

        self.assertEqual(host.cleanup(143), 143)

        self.assertEqual((host.arm.proc.returncode, host.successor.proc.returncode), (42, 42))
        self.assertTrue(engine.saw_term)
        self.assertIsNotNone(host.engine_proc.poll())
        self.assertFalse(os.path.exists(self.home.at(".branch-eligible-owner")))
        self.assertEqual(self.lease_holder("demo"), "")
        for path in (paths.host_record, paths.turn_file, paths.engine_pid_file, host.turn_result, host.turn_errors,
                     arm_out, successor_out):
            self.assertFalse(os.path.exists(path), path)

    def test_cleanup_after_a_stop_signal_runs_to_its_end(self):
        write(self.host.paths.host_record, f"host\t{self.host.pid}\tid\n")
        with self.assertRaises(Stop), STOPS.shielded():
            STOPS.handle(signal.SIGTERM)
        self.assertEqual(self.host.cleanup(143), 143)
        self.assertFalse(os.path.exists(self.host.paths.host_record))

    def test_cleanup_leaves_a_successors_record(self):
        record = "host\t1\tsuccessor\n"
        write(self.host.paths.host_record, record)
        self.assertEqual(self.host.cleanup(143), 143)
        self.assertEqual(files.read_text(self.host.paths.host_record), record)

    def test_a_host_stopped_before_it_activated_leaves_the_branch_leases(self):
        self.assertEqual(self.lease("branch", "demo").returncode, 0)
        self.assertEqual(self.host.cleanup(143), 143)
        self.assertTrue(self.lease_holder("demo").startswith("branch "))

    def test_cleanup_leaves_a_successors_leases_and_turn_records(self):
        paths = self.host.paths
        write(paths.host_record, "host\t1\tsuccessor\n")
        write(paths.turn_file, "turn=successor\n")
        write(paths.engine_pid_file, "1\tsuccessor\n")
        self.assertEqual(self.lease("branch", "demo").returncode, 0)
        self.assertEqual(self.host.cleanup(143), 143)
        self.assertTrue(self.lease_holder("demo").startswith("branch "))
        self.assertEqual((files.read_text(paths.turn_file), files.read_text(paths.engine_pid_file)),
                         ("turn=successor\n", "1\tsuccessor\n"))

    def test_a_recorded_successor_is_left_running_for_main(self):
        out = self.home.at(".supervision-host-arm.successor")
        write(out, "")
        successor = Child(self.child(), out)
        self.host.record_process("arm", successor.pid)
        self.host.successor = successor
        self.assertTrue(self.host.detach_successor())
        self.assertIsNone(self.host.successor)
        self.assertTrue(successor.alive())
        self.assertEqual(files.read_text(self.host.paths.left_record),
                         f"{successor.pid}\t{self.identity(successor.pid)}\n")
        self.assertEqual(records.rows(self.host.paths.host_record), [])

    def test_a_successor_that_cannot_be_recorded_stays_tracked_for_the_cleanup(self):
        os.mkdir(self.host.paths.left_record)
        successor = Child(self.child(), self.home.at(".supervision-host-arm.successor"))
        self.host.successor = successor
        self.host.reason = "signal: demo.status"
        self.assertFalse(self.host.detach_successor())
        self.assertIs(self.host.successor, successor)
        self.assertEqual(os.listdir(self.host.paths.left_record), [])
        self.assertIn("\tpass-through\tsuccessor-unrecorded\tsignal: demo.status\n",
                      files.read_text(self.host.paths.host_log))

    def test_activation_stops_what_a_predecessor_left_and_takes_the_record(self):
        predecessor, arm, stranger, engine = (self.home.blocker() for _ in range(4))
        write(self.host.paths.host_record,
              f"host\t{predecessor.pid}\t{self.identity(predecessor.pid)}\n"
              f"arm\t{arm.pid}\t{self.identity(arm.pid)}\n"
              f"arm\t{stranger.pid}\tnot this process\n")
        write(self.host.paths.engine_pid_file, f"{engine.pid}\t{self.identity(engine.pid)}\n")
        for name in (".supervision-host-arm.x", ".supervision-host-result.x", ".supervision-host-turn"):
            write(self.home.at(name), "")

        self.assertTrue(self.host.activate())

        for blocker in (predecessor, arm, engine):
            blocker.wait_gone()
            self.assertTrue(blocker.saw_term)
        self.assertTrue(pid_alive(stranger.pid))
        self.assertEqual([row[:2] for row in records.rows(self.host.paths.host_record)], [["host", self.host.pid]])
        for name in (".supervision-host.engine-pid", ".supervision-host-arm.x", ".supervision-host-result.x",
                     ".supervision-host-turn"):
            self.assertFalse(os.path.exists(self.home.at(name)), name)

    def test_activation_takes_over_a_live_left_arm_and_forgets_a_gone_one(self):
        left = self.home.blocker()
        write(self.host.paths.left_record, f"{left.pid}\t{self.identity(left.pid)}\n")
        self.assertTrue(self.host.activate())
        self.assertEqual(self.host.left_arm, left.pid)
        self.assertTrue(os.path.exists(self.host.paths.left_record))

        later = self.home.host()
        write(later.paths.left_record, f"{left.pid}\tnot this process\n")
        self.assertTrue(later.activate())
        self.assertEqual(later.left_arm, "")
        self.assertFalse(os.path.exists(later.paths.left_record))


class BoundaryTest(unittest.TestCase):
    """The park boundary, read from the test clock file instead of the clock."""

    def setUp(self):
        self.home = Home()
        self.clock = os.path.join(self.home.home, "park-clock")

    def tearDown(self):
        self.home.cleanup()

    def host(self, **extra):
        return self.home.host(FM_TEST_SEAM="1", FM_TEST_SUPERVISION_HOST_CLOCK=self.clock,
                              FM_SUPERVISION_HOST_PARK_SECONDS="100", **extra)

    def at(self, elapsed):
        write(self.clock, f"{elapsed}\n")

    def test_the_park_ends_at_its_boundary(self):
        host = self.host()
        self.at(99)
        self.assertFalse(host.boundary_reached())
        self.at(100)
        self.assertTrue(host.boundary_reached())

    def test_the_clock_file_needs_the_test_marker(self):
        host = self.home.host(FM_TEST_SUPERVISION_HOST_CLOCK=self.clock, FM_SUPERVISION_HOST_PARK_SECONDS="100")
        self.at(100)
        self.assertFalse(host.boundary_reached())

    def test_a_model_free_codex_park_restarts_its_quiet_clock_instead(self):
        host = self.host(FM_SUPERVISION_HOST_PRIMARY="codex", FM_CODEX_MODEL_FREE_PARK="1")
        self.at(100)
        self.assertFalse(host.boundary_reached())
        self.assertIn("\tquiet-boundary\tafter 100s; watcher retained\n", files.read_text(host.paths.host_log))
        self.assertFalse(host.turn_crosses_boundary())

    def test_a_turn_that_could_outlive_the_turn_limit_is_not_started(self):
        host = self.host(FM_SUPERVISION_HOST_TURN_TIMEOUT="40", FM_SUPERVISION_HOST_PARK_LIMIT="150")
        self.at(108)
        self.assertFalse(host.turn_crosses_boundary())
        self.at(109)
        self.assertTrue(host.turn_crosses_boundary())


if __name__ == "__main__":
    unittest.main()
