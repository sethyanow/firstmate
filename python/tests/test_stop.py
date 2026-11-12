"""The host's process, activation, and cleanup steps, on a fixture home with real processes."""

import dataclasses
import os
import signal
import subprocess
import unittest

from support import BIN, Home, write

from firstmate import files
from firstmate.host import records
from firstmate.host.cycles import Cycles, Slot
from firstmate.host.libs import HostLibs
from firstmate.host.lifecycle import activate, cleanup
from firstmate.host.procs import Child, Held, ProcessControl
from firstmate.processes import pid_alive
from firstmate.signals import Stop, Stops


class HostStepsTest(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.host = self.home.host()
        self.ctx, self.procs = self.host.ctx, self.host.procs
        self.paths = self.ctx.paths
        self.pid = self.ctx.facts.pid
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
        proc = subprocess.Popen(
            list(argv) or ["bash", "-c", "trap 'exit 42' TERM; read -r _"], stdin=subprocess.PIPE
        )
        self.children.append(proc)
        return proc

    def identity(self, pid):
        return self.ctx.libs.identity_of(pid)

    def lease(self, actor, task):
        env = self.home.env(FM_SUPERVISION_ACTOR=actor)
        return subprocess.run(
            [os.path.join(BIN, "fm-lease.sh"), "claim", task],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )

    def lease_holder(self, task):
        return subprocess.run(
            [os.path.join(BIN, "fm-lease.sh"), "check", task],
            env=self.home.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        ).stdout

    def own_record(self):
        write(self.paths.host_record, f"host\t{self.pid}\t{self.identity(self.pid)}\n")


class ProcessControlTest(HostStepsTest):
    def test_assembling_a_host_touches_nothing(self):
        self.assertEqual(os.listdir(self.home.state), [])

    def test_a_recorded_process_is_stopped_through_its_record_and_forgetting_keeps_the_rest(self):
        first = self.home.blocker()
        second = self.home.blocker()
        self.procs.record("arm", first.pid)
        self.procs.record("turn", second.pid)
        row = records.rows(self.paths.host_record)[0]
        self.assertEqual((row.role, row.pid), ("arm", first.pid))
        self.procs.stop_recorded(row.pid, row.identity, 1)
        first.wait_gone()
        self.assertTrue(first.saw_term)
        self.procs.forget(first.pid)
        self.assertEqual(
            [(row.role, row.pid) for row in records.rows(self.paths.host_record)],
            [("turn", second.pid)],
        )

    def test_stop_recorded_leaves_a_mismatched_or_unidentified_process_alone(self):
        blocker = self.home.blocker()
        self.procs.stop_recorded(blocker.pid, "a different identity", 1)
        self.procs.stop_recorded(blocker.pid, "", 1)
        blocker.release()
        blocker.wait_gone()
        self.assertEqual((blocker.saw_term, blocker.finished_normally), (False, True))

    def test_stop_recorded_kills_a_matching_process_that_ignores_term(self):
        blocker = self.home.blocker(trap="")
        self.procs.stop_recorded(blocker.pid, self.identity(blocker.pid), 0)
        blocker.wait_gone()
        self.assertEqual((blocker.saw_term, blocker.finished_normally), (False, False))

    def test_retiring_an_arm_stops_it_and_drops_its_row_and_output(self):
        out = self.home.at(".supervision-host-arm.fixture")
        write(out, "watcher: started pid=1\n")
        arm = Child(self.child(), out)
        self.procs.record("arm", arm.pid)
        self.procs.retire(arm)
        self.assertEqual(arm.proc.returncode, 42)
        self.assertEqual(records.rows(self.paths.host_record), [])
        self.assertFalse(os.path.exists(out))

    def test_releasing_branch_leases_keeps_main_leases(self):
        self.assertEqual(self.lease("branch", "demo").returncode, 0)
        self.assertEqual(self.lease("main", "other").returncode, 0)
        self.ctx.release_branch_leases()
        self.assertEqual(self.lease_holder("demo"), "")
        self.assertTrue(self.lease_holder("other").startswith("main "))


class EngineStopTest(HostStepsTest):
    def turn(self, *script_args):
        return self.child("bash", "-c", *script_args)

    def test_stopping_a_turn_signals_its_recorded_engine(self):
        engine = self.home.blocker()
        write(self.paths.engine_pid_file, f"{engine.pid}\t{self.identity(engine.pid)}\n")
        turn = self.turn(
            'trap "" TERM; while kill -0 "$1" 2>/dev/null; do sleep 0.05; done; exit 7',
            "_",
            str(engine.pid),
        )
        self.procs.stop_engine(turn)
        self.assertEqual(turn.returncode, 7)
        self.assertTrue(engine.saw_term)

    def test_stopping_a_turn_leaves_a_reused_engine_pid_alone(self):
        engine = self.home.blocker()
        write(self.paths.engine_pid_file, f"{engine.pid}\tforeign\n")
        turn = self.turn("exit 0")
        self.procs.stop_engine(turn)
        engine.release()
        engine.wait_gone()
        self.assertEqual((engine.saw_term, engine.finished_normally), (False, True))

    def test_an_engine_the_turn_records_while_the_stop_waits_is_stopped_too(self):
        first, second = self.home.blocker(), self.home.blocker()
        write(self.paths.engine_pid_file, f"{first.pid}\t{self.identity(first.pid)}\n")
        record = f"{second.pid}\t{self.identity(second.pid)}\n"
        # The turn records its next bounded process only once the first is gone.
        turn = self.turn(
            'trap "" TERM; while kill -0 "$1" 2>/dev/null; do sleep 0.05; done; '
            'printf "%s" "$3" > "$2"; while kill -0 "$4" 2>/dev/null; do sleep 0.05; done; exit 7',
            "_",
            str(first.pid),
            self.paths.engine_pid_file,
            record,
            str(second.pid),
        )
        self.procs.stop_engine(turn)
        self.assertEqual(turn.returncode, 7)
        self.assertEqual((first.saw_term, second.saw_term), (True, True))


class CleanupTest(HostStepsTest):
    def test_cleanup_stops_its_processes_releases_its_grant_and_leases(self):
        self.own_record()
        subprocess.run(
            [os.path.join(BIN, "fm-wake-grant.sh"), "activate", str(self.pid), self.ctx.facts.gen],
            env=self.home.env(),
            check=True,
            stdout=subprocess.DEVNULL,
        )
        self.assertEqual(self.lease("branch", "demo").returncode, 0)
        arm_out, successor_out = (
            self.home.at(".supervision-host-arm.a"),
            self.home.at(".supervision-host-arm.s"),
        )
        result, errors = (
            self.home.at(".supervision-host-result.x"),
            self.home.at(".supervision-host-errors.x"),
        )
        for path in (arm_out, successor_out, self.paths.turn_file, result, errors):
            write(path, "")
        engine = self.home.blocker()
        write(self.paths.engine_pid_file, f"{engine.pid}\t{self.identity(engine.pid)}\n")
        arm, successor = Child(self.child(), arm_out), Child(self.child(), successor_out)
        held = Held(
            arm=arm,
            successor=successor,
            engine=self.child(
                "bash",
                "-c",
                'trap "" TERM; while kill -0 "$1" 2>/dev/null; do sleep 0.05; done',
                "_",
                str(engine.pid),
            ),
            grant=True,
            turn_files=(result, errors),
        )

        cleanup(self.ctx, self.procs, held)

        self.assertEqual((arm.proc.returncode, successor.proc.returncode), (42, 42))
        self.assertTrue(engine.saw_term)
        self.assertFalse(os.path.exists(self.home.at(".branch-eligible-owner")))
        self.assertEqual(self.lease_holder("demo"), "")
        for path in (
            self.paths.host_record,
            self.paths.turn_file,
            self.paths.engine_pid_file,
            result,
            errors,
            arm_out,
            successor_out,
        ):
            self.assertFalse(os.path.exists(path), path)

    def test_cleanup_removes_only_turn_files_under_the_hosts_own_names(self):
        stranger = os.path.join(self.home.home, "not-the-hosts")
        write(stranger, "")
        cleanup(self.ctx, self.procs, Held(turn_files=(stranger,)))
        self.assertTrue(os.path.exists(stranger))

    def test_cleanup_leaves_a_successors_record_leases_and_turn_records(self):
        write(self.paths.host_record, "host\t1\tsuccessor\n")
        write(self.paths.turn_file, "turn=successor\n")
        write(self.paths.engine_pid_file, "1\tsuccessor\n")
        self.assertEqual(self.lease("branch", "demo").returncode, 0)
        cleanup(self.ctx, self.procs, Held())
        self.assertTrue(self.lease_holder("demo").startswith("branch "))
        self.assertEqual(
            [
                files.read_text(path)
                for path in (
                    self.paths.host_record,
                    self.paths.turn_file,
                    self.paths.engine_pid_file,
                )
            ],
            ["host\t1\tsuccessor\n", "turn=successor\n", "1\tsuccessor\n"],
        )

    def test_a_host_stopped_before_it_activated_leaves_the_branch_leases(self):
        self.assertEqual(self.lease("branch", "demo").returncode, 0)
        cleanup(self.ctx, self.procs, Held())
        self.assertTrue(self.lease_holder("demo").startswith("branch "))


class ActivationTest(HostStepsTest):
    def test_activation_stops_what_a_predecessor_left_and_takes_the_record(self):
        predecessor, arm, stranger, engine = (self.home.blocker() for _ in range(4))
        write(
            self.paths.host_record,
            f"host\t{predecessor.pid}\t{self.identity(predecessor.pid)}\n"
            f"arm\t{arm.pid}\t{self.identity(arm.pid)}\n"
            f"arm\t{stranger.pid}\tnot this process\n",
        )
        write(self.paths.engine_pid_file, f"{engine.pid}\t{self.identity(engine.pid)}\n")
        for name in (
            ".supervision-host-arm.x",
            ".supervision-host-result.x",
            ".supervision-host-turn",
        ):
            write(self.home.at(name), "")

        self.assertIsNotNone(activate(self.ctx, self.procs))

        for blocker in (predecessor, arm, engine):
            blocker.wait_gone()
            self.assertTrue(blocker.saw_term)
        self.assertTrue(pid_alive(stranger.pid))
        self.assertEqual(
            [(row.role, row.pid) for row in records.rows(self.paths.host_record)],
            [("host", self.pid)],
        )
        for name in (
            ".supervision-host.engine-pid",
            ".supervision-host-arm.x",
            ".supervision-host-result.x",
            ".supervision-host-turn",
        ):
            self.assertFalse(os.path.exists(self.home.at(name)), name)

    def test_activation_takes_over_a_live_left_arm_and_forgets_a_gone_one(self):
        left = self.home.blocker()
        write(self.paths.left_record, f"{left.pid}\t{self.identity(left.pid)}\n")
        activation = activate(self.ctx, self.procs)
        self.assertEqual(activation.left_arm if activation else None, left.pid)
        self.assertTrue(os.path.exists(self.paths.left_record))

        later = self.home.host()
        write(self.paths.left_record, f"{left.pid}\tnot this process\n")
        activation = activate(later.ctx, later.procs)
        self.assertEqual(activation.left_arm if activation else "failed", None)
        self.assertFalse(os.path.exists(self.paths.left_record))

    def test_a_hand_back_owner_record_that_cannot_be_read_stops_activation(self):
        write(self.paths.handoff_owner, "12")
        self.assertIsNone(activate(self.ctx, self.procs))
        self.assertFalse(os.path.exists(self.paths.host_record))


class SuccessorTest(HostStepsTest):
    def test_a_recorded_successor_is_left_running_for_main(self):
        out = self.home.at(".supervision-host-arm.successor")
        write(out, "")
        successor = Child(self.child(), out)
        self.procs.record("arm", successor.pid)
        held = Held(successor=successor)
        self.assertTrue(Cycles(self.ctx, self.procs, held).detach_successor("signal: demo.status"))
        self.assertIsNone(held.successor)
        self.assertTrue(successor.alive())
        self.assertEqual(
            files.read_text(self.paths.left_record),
            f"{successor.pid}\t{self.identity(successor.pid)}\n",
        )
        self.assertEqual(records.rows(self.paths.host_record), [])

    def test_a_successor_that_cannot_be_recorded_stays_held_for_the_cleanup(self):
        os.mkdir(self.paths.left_record)
        successor = Child(self.child(), self.home.at(".supervision-host-arm.successor"))
        held = Held(successor=successor)
        self.assertFalse(
            Cycles(self.ctx, self.procs, held).detach_successor("signal: demo.status")
        )
        self.assertIs(held.successor, successor)
        self.assertEqual(os.listdir(self.paths.left_record), [])
        self.assertIn(
            "\tpass-through\tsuccessor-unrecorded\tsignal: demo.status\n",
            files.read_text(self.paths.host_log) or "",
        )


@dataclasses.dataclass(frozen=True)
class StopOnFirstIdentity(HostLibs):
    """The library boundary, with a real TERM to this process when the host
    first reads a process identity: the moment an arm it started is recorded."""

    def identity_of(self, pid: int) -> str:
        if not os.path.exists(self.paths.at("stop-sent")):
            write(self.paths.at("stop-sent"), "")
            os.kill(os.getpid(), signal.SIGTERM)
        return super().identity_of(pid)


class ArmStartTest(HostStepsTest):
    def test_a_stop_while_an_arm_starts_leaves_the_arm_to_the_cleanup(self):
        bin_dir = os.path.join(self.home.home, "bin")
        os.mkdir(bin_dir)
        for name in os.listdir(BIN):
            if name != "fm-watch-arm.sh":
                os.symlink(os.path.join(BIN, name), os.path.join(bin_dir, name))
        write(os.path.join(bin_dir, "fm-watch-arm.sh"), "#!/usr/bin/env bash\nexec sleep 300\n")
        os.chmod(os.path.join(bin_dir, "fm-watch-arm.sh"), 0o755)
        saved = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, saved)
        stops = Stops()
        host = self.home.host(bin_dir, stops)
        libs = StopOnFirstIdentity(host.ctx.libs.bash, host.ctx.paths, host.ctx.facts.primary)
        ctx = dataclasses.replace(host.ctx, libs=libs)
        procs = ProcessControl(ctx.paths, libs, stops, 1)
        held = Held()
        write(ctx.paths.host_record, f"host\t{ctx.facts.pid}\tid\n")
        stops.install()
        with self.assertRaises(Stop):
            Cycles(ctx, procs, held).start(Slot.ARM, None)
        arm = held.arm
        self.assertIsNotNone(arm)
        if arm is not None:
            self.addCleanup(self.reap_if_left, arm.proc)
            cleanup(ctx, procs, held)
            self.assertIsNotNone(arm.proc.poll())

    @staticmethod
    def reap_if_left(proc):
        """Kill and reap an arm a failed run of the test above left running."""
        if proc.poll() is None:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    unittest.main()
