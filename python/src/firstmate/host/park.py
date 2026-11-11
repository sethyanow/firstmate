"""The supervision host's park loop; the firstmate.host docstring owns its contract."""

import contextlib
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass

from .. import files
from ..bashlib import CAPTURE, INHERIT, NULL, Bash
from ..signals import STOPS, Exit
from . import latch as latches
from . import records, text
from .settings import COOLDOWN_MAX, numeric_or, paths_from, settings_from

# The libraries the host's functions run with, in the order it sources them.
LIBRARIES = (
    "fm-wake-lib.sh",
    "fm-session-lock-lib.sh",
    "fm-timeout-lib.sh",
    "fm-supervision-engine-lib.sh",
    "fm-afk-contract.sh",
    "fm-backend.sh",
    "fm-supervisor-target-lib.sh",
    "fm-warm-compact-lib.sh",
)

IDENTITY_LIBRARIES = ("fm-wake-lib.sh", "fm-supervision-engine-lib.sh")

BOUNDARY_HANDBACK = ("supervision-host: cycle boundary - the host ended its park at its bound; "
                     "drain, acknowledge, and end the turn")
BOUNDARY_LINE = BOUNDARY_HANDBACK + ", and the next park starts on its own"
NO_LONGER_OWNER = "this session no longer owns supervision"


def pid_alive(pid):
    """True while a process answers to `pid` (`kill -0`)."""
    if not pid or not str(pid).isascii() or not str(pid).isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def signal_pid(pid, sig):
    try:
        os.kill(int(pid), sig)
        return True
    except (OSError, ValueError):
        return False


def run(argv, stdout=NULL, stderr=NULL, input_text=None, env=None, umask=-1):
    """Run a command to completion in the foreground; (status, captured output)."""
    with STOPS.shielded():
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input_text is not None else None,
                                    stdout=stdout, stderr=stderr, env=env, umask=umask)
        except FileNotFoundError:
            return 127, ""
        except OSError:
            return 126, ""
        out, _ = proc.communicate(files.encode(input_text) if input_text is not None else None)
    return proc.returncode, files.decode(out) if out else ""


def write_out(data):
    """Write to stdout in one call where the pipe allows, never split by a stop signal."""
    data = files.encode(data)
    with STOPS.shielded():
        while data:
            data = data[os.write(1, data):]


@dataclass
class Child:
    """A process this host started, with the file that captures its output."""

    proc: subprocess.Popen
    out: str

    @property
    def pid(self):
        return str(self.proc.pid)

    def alive(self):
        return self.proc.poll() is None


class Host:
    def __init__(self, env, script_dir, restart):
        self.env = env
        self.paths = paths_from(env, script_dir)
        self.settings = settings_from(env)
        self.restart = restart
        self.bash = Bash(script_dir, LIBRARIES, self.paths.script("fm-supervision-host.sh"), {
            "FM_ROOT": self.paths.root,
            "FM_HOME": self.paths.home,
            "STATE": self.paths.state,
            "CONFIG": self.paths.config,
            "SCRIPT_DIR": script_dir,
        }, env)
        self.grace = env.get("FM_GUARD_GRACE") or self.bash.call("fm_poll_derived_grace").text
        self.cooldown = int(self.bash.value("FM_SUPERVISION_HOST_COOLDOWN"))
        self.autoarm_gen = env.get("FM_SUPERVISION_HOST_AUTOARM_GEN", "")
        self.autoarm_owner = env.get("FM_SUPERVISION_HOST_OWNER_PID", "")
        self.primary = env.get("FM_SUPERVISION_HOST_PRIMARY") or self.bash.call("fm_supervision_host_primary").text
        # The owner's predecessor arm belongs to the first cycle only.
        predecessor = env.get("FM_WATCH_PREDECESSOR_ARM_PID", "")
        self.owner_predecessor = predecessor if predecessor.isascii() and predecessor.isdigit() else ""
        for name in ("FM_WATCH_PREDECESSOR_ARM_PID", "FM_SUPERVISION_ACTOR", "FM_BRANCH_REPORT_TURN"):
            env.pop(name, None)

        self.pid = str(os.getpid())
        self.park_started = time.monotonic()
        self.gen = f"host-{self.pid}-{int(time.time())}"
        self.turn_seq = 0
        self.last_turn = ""
        self.turn_posture = ""
        self.engine_error = False
        self.health_note = ""
        self.health = latches.Latch()
        self.grant_active = False
        self.arm = None
        self.arm_text = ""
        self.closed_arm_pid = ""
        self.handle_why = ""
        self.engine_proc = None
        self.engine_running = False
        self.successor = None
        self.successor_watcher = ""
        self.successor_generation = ""
        # The successor arm a predecessor's pass-through left for main, which
        # the first cycle takes over.
        self.left_arm = ""
        # The running turn's result and diagnostics files, which the cleanup
        # removes when the host is stopped mid-turn.
        self.turn_result = ""
        self.turn_errors = ""
        # The first cycle's status line, printed as soon as the arm prints it
        # and left out of that cycle's close.
        self.ready_pending = True
        self.ready_line = ""
        self.reason = ""
        self.engine = ""
        self.engine_model = ""
        self.engine_problem = ""
        self.record_key = ""
        self.record_session = ""
        self.record_turns = 0
        self.engine_session = ""
        self.engine_mode = ""
        self.engine_turns = 0
        self.engine_cost = ""
        self.engine_key = ""
        self.attended_why = ""
        self.attended_offer = ""
        self.turn_receipt_seqs = ""
        self.returned_rows = ""
        self.returned_seqs = ""
        self.returned_lookup_failed = False
        self.warm_last_tick = -1
        self.warm_primary_done = False
        self.warm_verdict = ""
        self.cleanup_armed = False

    # --- child commands ------------------------------------------------------

    def run(self, argv, env=None, **options):
        return run(argv, env=self.env if env is None else env, **options)

    def private_run(self, argv, out_path, input_text=None):
        """Run a command whose output may carry the captain's words into an owner-only file."""
        try:
            fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        except OSError:
            return 1
        try:
            return self.run(argv, stdout=fd, input_text=input_text, umask=0o077)[0]
        finally:
            os.close(fd)


    def call(self, func, *args, **options):
        return self.bash.call(func, *args, **options)

    def host_config(self):
        """fm_supervision_host_config: True when the home runs the host; sets the engine."""
        result = self.call("fm_supervision_host_config", self.paths.config, self.primary, stdout=INHERIT,
                           out_vars=("FM_SUPERVISION_ENGINE", "FM_SUPERVISION_ENGINE_MODEL",
                                     "FM_SUPERVISION_ENGINE_PROBLEM"))
        self._take_engine(result.vars)
        return result.ok

    def _take_engine(self, values):
        self.engine = values.get("FM_SUPERVISION_ENGINE", "")
        self.engine_model = values.get("FM_SUPERVISION_ENGINE_MODEL", "")
        self.engine_problem = values.get("FM_SUPERVISION_ENGINE_PROBLEM", "")
        self.bash.globals.update({
            "FM_SUPERVISION_ENGINE": self.engine,
            "FM_SUPERVISION_ENGINE_MODEL": self.engine_model,
            "FM_SUPERVISION_ENGINE_PROBLEM": self.engine_problem,
        })

    def away_present(self):
        return self.call("fm_afk_contract_away_present", self.paths.state, stdout=INHERIT).ok

    def main_key(self):
        result = self.call("fm_supervision_host_main_key", self.paths.state)
        return result.text if result.ok else None

    def identity_of(self, pid):
        """A process's identity (bin/fm-wake-lib.sh fm_pid_identity), read once a
        poll while parked, so it sources only the two libraries it needs."""
        if not pid:
            return ""
        return self.call("_fm_engine_identity", pid, libraries=IDENTITY_LIBRARIES).text

    def publish_downtime(self, quiet=True):
        return self.call("fm_recovery_marker_publish", self.paths.marker, "downtime", stdout=NULL,
                         stderr=NULL if quiet else INHERIT).ok

    def release_branch_leases(self):
        self.run([self.paths.script("fm-lease.sh"), "release-actor", "--actor", "branch"],
            env={**self.env, "FM_SUPERVISION_ACTOR": "branch"})

    def grant(self, *args):
        return self.run([self.paths.script("fm-wake-grant.sh"), *args])[0]

    def stop_watcher(self):
        self.run([self.paths.script("fm-watch-arm.sh"), "--stop"])

    # --- the record and ledger ---------------------------------------------

    def log_line(self, line):
        with STOPS.shielded():
            records.log(self.paths.host_log, line, int(time.time()))

    def record_process(self, role, pid):
        """Record one process this host runs, so a successor host can stop exactly it."""
        identity = self.identity_of(pid)
        with STOPS.shielded():
            records.append(self.paths.host_record, role, pid, identity)

    def refresh_process(self, pid):
        """Re-record this host's own unreaped child under its current identity: its
        first identity may have been read between its fork and its exec."""
        if not pid or not os.path.isfile(self.paths.host_record):
            return
        identity = self.identity_of(pid)
        with STOPS.shielded():
            records.refresh(self.paths.host_record, pid, identity)

    def forget_process(self, pid):
        with STOPS.shielded():
            records.forget(self.paths.host_record, pid)

    def host_record_is_ours(self):
        return records.host_pid(self.paths.host_record) == self.pid

    # --- stopping processes -------------------------------------------------

    def stop_recorded(self, pid, identity, seconds):
        """Stop a recorded process only while it still answers to its recorded
        identity: TERM, then KILL once `seconds` pass."""
        if not pid_alive(pid) or not identity or self.identity_of(pid) != identity:
            return
        if not signal_pid(pid, signal.SIGTERM):
            return
        for _ in range(seconds * 10):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
        if pid_alive(pid) and self.identity_of(pid) == identity:
            signal_pid(pid, signal.SIGKILL)

    def stop_engine_turn(self):
        """Stop a running engine turn: TERM the bounded process, whose watchdog
        passes it on and KILLs after its grace, then let the turn's own poller
        reap the engine's descendants before giving up on it. A turn stopped
        before it recorded its bounded process records it while the host waits,
        and that process is stopped then."""
        seen = set()
        self.signal_recorded_engine(seen)
        if self.engine_proc is None:
            return
        deadline = time.monotonic() + self.settings.engine_grace + 10
        while self.engine_proc.poll() is None and time.monotonic() < deadline:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.engine_proc.wait(timeout=0.1)
            self.signal_recorded_engine(seen)
        if self.engine_proc.poll() is None:
            self.engine_proc.kill()
            self.engine_proc.wait()

    def signal_recorded_engine(self, seen):
        """TERM the bounded process the engine pid file names while it still
        answers to its recorded identity, judging each record once."""
        record = tuple(files.tsv(files.first_line(self.paths.engine_pid_file), 2))
        if record in seen:
            return
        seen.add(record)
        pid, identity = record
        if pid and pid_alive(pid) and self.identity_of(pid) == identity:
            signal_pid(pid, signal.SIGTERM)

    def retire_arm(self, child):
        """Stop one arm this host started (its TERM handler stops the watcher it
        owns) and drop its output file."""
        if child is None:
            return
        if child.alive():
            child.proc.terminate()
            try:
                child.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.proc.kill()
                child.proc.wait()
        self.forget_process(child.pid)
        files.remove(child.out)

    def retire_successor(self):
        """Stop the successor cycle: the state main's own turn end starts from
        without the host."""
        if self.successor is None:
            return
        self.retire_arm(self.successor)
        self.successor = None
        self.stop_watcher()

    def cleanup(self, status):
        STOPS.disarm()
        if self.engine_running:
            self.stop_engine_turn()
        self.retire_arm(self.successor)
        self.retire_arm(self.arm)
        if self.grant_active:
            self.grant("release", self.gen)
            self.grant("deactivate", self.pid, self.gen)
        for path in (self.turn_result, self.turn_errors):
            if path.startswith(self.paths.at(".supervision-host-")):
                files.remove(path)
        # The branch leases and the turn records belong to the host whose row
        # the record holds: none to a host stopped before it activated, and a
        # successor's once that successor activated.
        if self.host_record_is_ours():
            self.release_branch_leases()
            files.remove(self.paths.turn_file, self.paths.engine_pid_file, self.paths.host_record)
        return status

    # --- ownership and activation -------------------------------------------

    def host_still_owner(self):
        if not self.call("fm_session_lock_owned_by_self", self.paths.state, stdout=INHERIT).ok:
            return False
        if not self.autoarm_gen:
            return True
        ledger = self.call("fm_autoarm_ledger_read", self.paths.state, stdout=INHERIT,
                           out_vars=("FM_AUTOARM_GEN", "FM_AUTOARM_OWNER", "FM_AUTOARM_OUTCOME"))
        return (ledger.ok and ledger.vars["FM_AUTOARM_GEN"] == self.autoarm_gen
                and ledger.vars["FM_AUTOARM_OWNER"] == self.autoarm_owner
                and ledger.vars["FM_AUTOARM_OUTCOME"] == "arming")

    def activate(self):
        """Stop whatever a predecessor host left running, then take the record.
        The auto-arm admits one generation at a time, so a predecessor still
        alive here was superseded (its owner died or went stale) or crashed
        mid-cleanup."""
        paths = self.paths
        try:
            os.makedirs(paths.state, exist_ok=True)
        except OSError:
            return False
        # The predecessor host first, with room for its own cleanup (which
        # stops its engine and arms), before anything it left is stopped
        # individually.
        for role, pid, identity in records.rows(paths.host_record):
            if role == "host" and pid != self.pid:
                self.stop_recorded(pid, identity, self.settings.engine_grace + 20)
        handoff_owner = paths.at(".supervision-handoff-owner")
        if os.path.isfile(handoff_owner):
            owner = files.complete_lines(files.read_text(handoff_owner) or "")
            if not owner:
                return False
            pid, identity = files.tsv(owner[0], 2)
            self.stop_recorded(pid, identity, 10)
            if pid_alive(pid) and self.identity_of(pid) == identity:
                return False
        if os.path.isfile(paths.engine_pid_file):
            pid, identity = files.tsv(files.first_line(paths.engine_pid_file), 2)
            self.stop_recorded(pid, identity, self.settings.engine_grace + 5)
            files.remove(paths.engine_pid_file)
        for role, pid, identity in records.rows(paths.host_record):
            if role == "arm":
                self.stop_recorded(pid, identity, 10)
        # A predecessor killed outright ran no cleanup: reap the engine
        # descendants its turn recorded, then drop that turn's files.
        state_files = sorted(os.listdir(paths.state))
        for name in state_files:
            if name.startswith(".supervision-host-descendants.") and not name.endswith((".pids", ".next")) \
                    and os.path.isfile(paths.at(name)):
                self.call("_fm_engine_reap", paths.at(name), stdout=INHERIT)
        for name in os.listdir(paths.state):
            if name.startswith(tuple(f".supervision-host-{kind}." for kind in
                                     ("arm", "warm", "descendants", "result", "errors", "readback"))):
                files.remove(paths.at(name))
        files.remove(paths.turn_file, paths.mirror_feed)
        # The successor a pass-through left for main: the first cycle takes it
        # over while it still answers to its recorded identity, and its record
        # goes only once it does not.
        if os.path.isfile(paths.left_record):
            pid, identity = files.tsv(files.first_line(paths.left_record), 2)
            if pid_alive(pid) and identity and self.identity_of(pid) == identity:
                self.left_arm = pid
            else:
                files.remove(paths.left_record)
        identity = self.identity_of(self.pid)
        try:
            with STOPS.shielded(), open(paths.host_record, "wb") as handle:
                handle.write(files.encode(f"host\t{self.pid}\t{identity}\n"))
        except OSError:
            return False
        self.release_branch_leases()
        return True

    # --- watcher cycles -------------------------------------------------------

    def start_arm(self, slot, predecessor, *args, own_group=False):
        """Start one watcher cycle as this host's `slot` ("arm" or "successor");
        the started Child, or None. The slot holds the child before a stop
        signal can end the start, so the cleanup stops it."""
        env = {**self.env, "FM_GUARD_GRACE": self.grace}
        if predecessor:
            env["FM_WATCH_PREDECESSOR_ARM_PID"] = predecessor
        with STOPS.shielded():
            try:
                fd, out = files.mkstemp(self.paths.at(".supervision-host-arm"))
            except OSError:
                return None
            try:
                # An arm left for main outlives this host, and Claude tears the
                # hook's process group down after the exit-2 rewake, so it gets
                # a group of its own (the shape start_handling_successor in
                # bin/fm-claude-stop-autoarm.sh uses).
                proc = subprocess.Popen([self.paths.script("fm-watch-arm.sh"), *args], stdin=subprocess.DEVNULL,
                                        stdout=fd, stderr=subprocess.STDOUT, env=env,
                                        process_group=0 if own_group else None)
            except OSError:
                files.remove(out)
                return None
            finally:
                os.close(fd)
            child = Child(proc, out)
            setattr(self, slot, child)
            self.record_process("arm", child.pid)
        return child

    def stream_ready_line(self):
        """Print the first cycle's status line once the arm has written it in full."""
        line = text.status_line(files.read_text(self.arm.out) or "")
        if not line:
            return
        write_out(line + "\n")
        self.ready_line = line
        self.ready_pending = False

    def await_close(self):
        """Wait for the current arm to close; False when the park boundary arrives first."""
        while self.arm.alive():
            self.refresh_process(self.arm.pid)
            if self.ready_pending:
                self.stream_ready_line()
            if self.boundary_reached():
                return False
            self.warm_compact_tick()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.arm.proc.wait(timeout=self.settings.poll)
        close = files.output(files.read_text(self.arm.out) or "")
        if self.ready_line:
            # Already printed: drop its first occurrence from this first close.
            close = text.without_status(close, self.ready_line)
            self.ready_line = ""
        self.ready_pending = False
        self.forget_process(self.arm.pid)
        files.remove(self.arm.out)
        self.arm_text = close
        self.closed_arm_pid = self.arm.pid
        self.arm = None
        return True

    def start_successor(self, predecessor):
        """Start the successor cycle and wait until it proves a live watcher.
        Sets the successor's watcher and recovery generation (empty when the arm attached)."""
        self.successor_watcher = ""
        self.successor_generation = ""
        child = self.start_arm("successor", predecessor, own_group=True)
        if child is None:
            return False
        deadline = time.monotonic() + self.settings.ready_timeout
        while True:
            verified = text.verified_watcher(files.read_text(child.out) or "")
            if verified:
                self.successor_watcher, self.successor_generation = verified
                self.refresh_process(child.pid)
                return True
            if not child.alive() or time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def detach_successor(self):
        """Record the successor for the next host to take over, then drop it from
        this host's cleanup without stopping it. A successor whose record does not
        read back as a regular file holding exactly its pid and identity stays
        tracked, so the cleanup stops it and main's next turn end arms a fresh
        cycle. Its capture file goes now: the arm keeps writing to the
        descriptor it already holds."""
        if self.successor is None:
            return True
        pid = self.successor.pid
        identity = self.identity_of(pid)
        expected = f"{pid}\t{identity}"
        with STOPS.shielded():
            recorded = bool(identity) and files.replace(self.paths.left_record, files.encode(expected + "\n")) \
                and not os.path.islink(self.paths.left_record) and os.path.isfile(self.paths.left_record) \
                and files.output(files.read_text(self.paths.left_record) or "") == expected
        if not recorded:
            self.log_line(f"pass-through\tsuccessor-unrecorded\t{text.first(self.reason)}")
            return False
        self.forget_process(pid)
        files.remove(self.successor.out)
        self.successor = None
        return True

    def leave_successor_for_main(self):
        """Main receives this close only on downtime, even when a preceding engine
        turn left handling behind (autoarm_commit in bin/fm-claude-stop-autoarm.sh).
        1 on successor failure, 2 on an undeliverable recovery marker."""
        if not self.start_successor(self.closed_arm_pid):
            self.log_line(f"pass-through\tsuccessor-unverified\t{text.first(self.reason)}")
            return 1
        if self.successor_generation and not self.publish_downtime():
            self.log_line(f"pass-through\tdowntime-unrestored\t{text.first(self.reason)}")
            return 2
        return 0 if self.detach_successor() else 1

    # --- the park boundary ----------------------------------------------------

    def park_elapsed(self):
        if self.env.get("FM_TEST_SEAM") == "1" and self.env.get("FM_TEST_SUPERVISION_HOST_CLOCK"):
            return numeric_or(files.output(files.read_text(self.env["FM_TEST_SUPERVISION_HOST_CLOCK"]) or ""), 0)
        return int(time.monotonic() - self.park_started)

    def model_free_park(self):
        return self.primary == "codex" and self.env.get("FM_CODEX_MODEL_FREE_PARK", "0") == "1"

    def boundary_reached(self):
        elapsed = self.park_elapsed()
        # The verified Codex async owner has no short hook deadline. Its quiet
        # checkpoint keeps the same watcher alive; stopping it would create a
        # real downtime generation and force an otherwise empty model turn.
        if self.model_free_park() and elapsed >= self.settings.park_seconds:
            self.log_line(f"quiet-boundary\tafter {elapsed}s; watcher retained")
            self.park_started = time.monotonic()
            self.warm_last_tick = -1
            return False
        return elapsed >= self.settings.park_seconds

    def turn_crosses_boundary(self):
        """True when an engine turn started now could still be running at the turn
        limit (the boundary unless the owner set a later one)."""
        if self.model_free_park():
            return False
        return (self.park_elapsed() + self.settings.turn_timeout + self.settings.engine_grace
                >= self.settings.park_limit)

    def boundary_exit(self):
        """End the park at the boundary: stop the current and successor arms and
        this home's watcher, print any close already read so main drains it,
        then the boundary line."""
        self.log_line(f"boundary\tafter {self.park_elapsed()}s")
        self.acknowledged_handback(BOUNDARY_HANDBACK)
        self.retire_arm(self.arm)
        self.retire_arm(self.successor)
        self.arm = None
        self.successor = None
        self.stop_watcher()
        self.emit(BOUNDARY_LINE)
        raise Exit(0)

    # --- warm-cache compaction ------------------------------------------------
    # bin/fm-warm-compact-lib.sh owns the gate and the guards. The park is the
    # primary's idle episode: the owner starts the host at the primary's turn
    # end and the host exits when main is needed. The engine conversation's
    # episode is the time since its last handled turn. Each is compacted at most
    # once per episode, and neither result reaches the close.

    def warm_compact_tick(self):
        if not os.path.isfile(os.path.join(self.paths.config, "warm-compact")):
            return
        elapsed = self.park_elapsed()
        if self.warm_last_tick >= 0 and elapsed - self.warm_last_tick < self.settings.warm_compact_tick:
            return
        self.warm_last_tick = elapsed
        if self.turn_crosses_boundary():
            return
        transcript = self.env.get("FM_WARM_COMPACT_TRANSCRIPT", "")
        if self.primary == "claude" and not self.warm_primary_done and transcript:
            target = self.call("discover_supervisor_target")
            backend = self.call("discover_supervisor_backend") if target.ok else None
            if backend is not None and backend.ok:
                verdict = self.call("fm_warm_compact_primary", self.paths.config, backend.text, target.text,
                                    elapsed, transcript, 0).text
                if verdict in ("sent", "failed"):
                    self.warm_primary_done = True
                    self.log_line(f"warm-compact\tprimary\t{verdict}")
        if self.host_config() and self.engine and self.engine_record_resumable():
            ended = numeric_or(files.field(self.paths.engine_record, "ended"), 0)
            if ended <= 0:
                return
            compacted = 1 if files.field(self.paths.engine_record, "compacted") == str(ended) else 0
            self.run_warm_compact_engine(int(time.time()) - ended, self.record_session, compacted)
            if self.warm_verdict in ("compacted", "failed"):
                self.mark_engine_compacted(ended)
                self.log_line(f"warm-compact\tengine\t{self.warm_verdict}")

    def run_warm_compact_engine(self, idle, session, compacted):
        """Run the engine compaction as a bounded background turn the cleanup can
        stop, and abandon it the moment the arm closes or the park boundary
        arrives, so a real wake never waits behind it. Sets the verdict, empty
        when abandoned."""
        self.warm_verdict = ""
        try:
            fd, verdict_file = files.mkstemp(self.paths.at(".supervision-host-warm.verdict"))
        except OSError:
            return
        abandoned = False
        with STOPS.shielded():
            self.engine_running = True
            self.engine_proc = self.bash.start(
                "fm_warm_compact_engine", self.paths.config, idle, session, self.engine, self.engine_model,
                self.paths.prompt_file, compacted, self.paths.engine_pid_file, stdout=fd, env=self.engine_env())
            os.close(fd)
        while self.engine_proc.poll() is None:
            if not self.arm.alive() or self.boundary_reached():
                abandoned = True
                self.stop_engine_turn()
                break
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.engine_proc.wait(timeout=0.5)
        self.engine_proc.wait()
        self.engine_proc = None
        self.engine_running = False
        files.remove(self.paths.engine_pid_file)
        if not abandoned:
            self.warm_verdict = files.output(files.read_text(verdict_file) or "")
        files.remove(verdict_file)

    def mark_engine_compacted(self, ended):
        content = files.read_text(self.paths.engine_record) or ""
        kept = [line for line in files.lines(content) if not line.startswith("compacted=")]
        with STOPS.shielded():
            files.replace(self.paths.engine_record,
                          files.encode("".join(line + "\n" for line in kept) + f"compacted={ended}\n"))

    # --- output and hand-back -------------------------------------------------

    def emit(self, *lines):
        """Print the close read so far, then the given lines, in one write, so an
        owner reading a stream sees the whole exit at once."""
        composed = text.compose(self.arm_text, lines)
        if composed:
            write_out(composed + "\n")

    def stand_down(self, why):
        self.log_line(f"stand-down\t{why}")
        self.emit(f"supervision-host stood down: {why}")
        raise Exit(0)

    def exit_to_main(self, why, further=""):
        """Hand the close to main: stop the successor cycle, print the close, why,
        and any further "supervision-host:" lines, and exit."""
        self.acknowledged_handback(f"supervision-host: {why}", further)
        lines = further
        status = 0
        self.retire_successor()
        if self.successor_generation and not self.publish_downtime():
            self.log_line(f"to-main\tdowntime-unrestored\t{why}")
            lines = (lines + "\n" if lines else "") + \
                "supervision-host: watcher downtime could not be restored for the main hand-back"
            status = 1
        self.log_line(f"to-main\t{why}")
        self.emit(f"supervision-host: {why}", lines)
        raise Exit(status)

    def acknowledged_handback(self, *lines):
        """The opt-in path never changes the default hand-back paths. It verifies
        protection before emitting the close rather than assuming a detached
        process started. Returns only when the opt-in is off."""
        if self.primary != "claude" or not os.path.exists(os.path.join(self.paths.config, "supervision-handoff-ack")):
            return
        if not self.host_still_owner():
            self.stand_down(NO_LONGER_OWNER)
        predecessor = (self.successor.pid if self.successor else "") or (self.arm.pid if self.arm else "") \
            or self.closed_arm_pid or self.pid
        self.retire_arm(self.arm)
        self.arm = None
        self.retire_successor()
        pending = self.paths.at(".supervision-handoff-pending")
        with STOPS.shielded():
            if not files.replace(pending, files.encode(self.gen + "\n")):
                raise Exit(1)
        appended = self.call(
            "fm_wake_append", "check", f"supervision-handoff:{self.gen}",
            "check: supervision-handoff - the host handed supervision to main; drain, handle, and acknowledge "
            "this hand-back", stdout=INHERIT).ok
        if not appended or not self.publish_downtime(quiet=False):
            self.emit(*lines, "supervision-host: the acknowledged hand-back could not be recorded")
            raise Exit(1)
        try:
            fd, out = files.mkstemp(self.paths.at(".supervision-handoff-start"))
        except OSError:
            raise Exit(1) from None
        with STOPS.shielded():
            try:
                proc = subprocess.Popen(
                    ["nohup", self.paths.script("fm-supervision-handoff.sh"), self.gen, predecessor],
                    stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT, process_group=0,
                    env={**self.env, "FM_SUPERVISION_HOST_READY_TIMEOUT": str(self.settings.ready_timeout)})
            except OSError:
                proc = None
            os.close(fd)
            if proc is not None:
                self.record_process("handoff", str(proc.pid))
        deadline = time.monotonic() + self.settings.ready_timeout
        while proc is not None and proc.poll() is None and time.monotonic() < deadline:
            if text.verified_watcher(files.read_text(out) or "") and self.call(
                    "fm_watcher_healthy", self.paths.state, self.paths.script("fm-watch.sh"), self.grace,
                    self.paths.home, stdout=INHERIT).ok:
                self.forget_process(str(proc.pid))
                files.remove(out)
                self.log_line(f"handoff\t{self.gen}")
                self.emit(*lines)
                raise Exit(0)
            time.sleep(0.2)
        if proc is not None:
            proc.terminate()
        files.remove(out)
        self.emit(*lines, "supervision-host: the acknowledged hand-back successor could not be verified")
        raise Exit(1)

    # --- the engine conversation ----------------------------------------------

    def engine_record_resumable(self):
        """True, setting the record's key, session, and turns, when the recorded
        conversation is one the next turn resumes."""
        record = self.paths.engine_record
        key = self.main_key() or ""
        self.record_key = key
        self.record_session = files.field(record, "session")
        self.record_turns = numeric_or(files.field(record, "turns"), 0)
        return bool(key and self.record_session and files.field(record, "key") == key
                    and files.field(record, "engine") == self.engine
                    and files.field(record, "model") == self.engine_model
                    and self.record_turns < self.settings.rotate_turns
                    and os.path.isfile(self.paths.prompt_file) and os.path.getsize(self.paths.prompt_file) > 0)

    def choose_conversation(self):
        """The engine conversation for this turn: the recorded one while it belongs
        to this main session and has turns left, otherwise a new one."""
        if self.engine_record_resumable():
            self.engine_session = self.record_session
            self.engine_mode = "resume"
            self.engine_turns = self.record_turns
            self.engine_cost = files.field(self.paths.engine_record, "conversation_cost")
            self.engine_key = self.record_key
            return True
        self.engine_session = str(uuid.uuid4())
        self.engine_mode = "new"
        self.engine_turns = 0
        self.engine_cost = "0"
        self.engine_key = self.record_key
        try:
            fd, tmp = files.mkstemp(self.paths.prompt_file + ".tmp")
        except OSError:
            return False
        with STOPS.shielded():
            rc, _ = self.run([self.paths.script("fm-branch-prompt.sh")], stdout=fd)
            os.close(fd)
            if rc != 0 or os.path.getsize(tmp) < 1024:
                files.remove(tmp)
                return False
            try:
                os.replace(tmp, self.paths.prompt_file)
            except OSError:
                files.remove(tmp)
                return False
        return True

    def write_engine_record(self, turns, cost):
        with STOPS.shielded():
            return files.replace(self.paths.engine_record, files.encode(
                f"engine={self.engine}\nmodel={self.engine_model}\nsession={self.engine_session}\n"
                f"key={self.engine_key}\nturns={turns}\nconversation_cost={cost}\nended={int(time.time())}\n"))

    # --- the broken-session latch (docs/supervision-host.md) -------------------

    def health_key(self):
        """The latch's key; the main-session part keeps a recycled lock pid from
        inheriting another session's conversation or latch."""
        result = self.call("fm_supervision_host_health_key", self.paths.state)
        return result.text if result.ok else None

    def health_cooling(self):
        """True while the latch holds main to every wake. Needs the engine config."""
        self.health = latches.load(self.paths.health_file, self.health_key())
        paused = self.call("fm_supervision_host_paused_until", self.paths.state)
        return paused.ok and paused.text.isdigit() and int(time.time()) < int(paused.text)

    def health_record(self, engine_error, reports):
        """Fold one finished turn into the latch, setting the one line main is owed
        when it trips for the first time."""
        key = self.health_key()
        self.health, self.health_note, logged = latches.after_turn(
            latches.load(self.paths.health_file, key), engine_error, reports, int(time.time()), self.cooldown,
            COOLDOWN_MAX)
        for line in logged:
            self.log_line(line)
        with STOPS.shielded():
            latches.save(self.paths.health_file, key, self.health)

    # --- one wake ---------------------------------------------------------------

    def attended_acceptor(self, first):
        """Why an attended close stays with main exactly as the plain arm delivers
        it, or empty when the supervision session may take it; sets the offer's
        verdict and the scope it judged."""
        self.attended_why = ""
        offer = ""
        ready = self.call("fm_supervision_host_attended_ready", self.paths.config, self.primary, stdout=INHERIT,
                          out_vars=("FM_SUPERVISION_HOST_UNREADY", "FM_SUPERVISION_ENGINE",
                                    "FM_SUPERVISION_ENGINE_MODEL", "FM_SUPERVISION_ENGINE_PROBLEM"))
        self._take_engine(ready.vars)
        if not ready.ok:
            self.attended_why = ready.vars["FM_SUPERVISION_HOST_UNREADY"]
        elif not self.call("fm_supervision_host_main_key", self.paths.state, stdout=NULL).ok:
            self.attended_why = "the main session could not be identified"
        elif self.health_cooling():
            self.attended_why = "the supervision session is cooling down after engine errors"
        else:
            rc, offer = self.run(["node", self.paths.script("fm-branch-dispatch.mjs"), "offer"], stdout=CAPTURE,
                            input_text=first + "\n")
            offer = files.output(offer)
            if rc != 0:
                self.attended_why = "branch eligibility could not be computed"
            elif _scope_field(offer, "eligible") != "1":
                self.attended_why = "main-only"
        self.attended_offer = offer
        return not self.attended_why

    def handle_wake(self, reason):
        """Handle one close on the engine, in the posture the record gives when the
        turn starts. 0 when the wake is handled (or held nothing the branch may
        claim); 2 when the turn starts attended and the supervision session may
        not take the close; otherwise 1 with the reason set."""
        self.last_turn = ""
        self.engine_error = False
        self.health_note = ""
        self.turn_posture = "away" if self.away_present() else "attended"
        first = text.first(reason)
        if self.turn_posture == "attended":
            if not self.attended_acceptor(first):
                return 2
            scope = self.attended_offer
        else:
            args = ["--heartbeat"] if first.startswith("heartbeat") else []
            rc, scope = self.run(["node", self.paths.script("fm-branch-dispatch.mjs"), "scope", *args, "--afk"],
                            stdout=CAPTURE)
            scope = files.output(scope)
            if rc != 0:
                self.handle_why = "branch eligibility could not be computed"
                return 1
        status = _scope_field(scope, "status")
        rows = _scope_field(scope, "rows")
        tasks = _scope_field(scope, "tasks")
        unscoped = _scope_field(scope, "unscoped")
        if _scope_field(scope, "corrupted") == "1":
            self.handle_why = "a queued wake could not be read or resolved to a task record, so its scope is unknown"
            return 1
        if status == "empty" or not rows:
            self.log_line(f"no-op\tnothing for the branch to claim\t{first}")
            return 0
        if not self.grant_active:
            if self.grant("activate", self.pid, self.gen) != 0:
                self.handle_why = "the branch grant could not be activated"
                return 1
            self.grant_active = True
        rc = self.grant("publish", self.gen, *rows.split())
        if rc == 3:
            self.handle_why = "main already claimed these wake rows"
            return 1
        if rc != 0:
            self.handle_why = "the branch grant could not be published"
            return 1
        if not self.host_still_owner():
            self.grant("release", self.gen)
            self.handle_why = NO_LONGER_OWNER
            return 1
        if not self.choose_conversation():
            self.grant("release", self.gen)
            self.handle_why = "the branch prompt or engine conversation could not be prepared"
            return 1
        self.turn_seq += 1
        turn = f"{self.gen}.{self.turn_seq}"
        self.last_turn = turn
        paths = self.paths
        with STOPS.shielded():
            _write(paths.receipts, "")
            _write(paths.turn_file, f"turn={turn}\nrows={rows}\ntasks={tasks}\nunscoped={unscoped or 0}\n"
                                    f"wake={first}\nposture={self.turn_posture}\n")
        readback = ""
        if self.turn_posture == "away":
            try:
                fd, readback = files.mkstemp(paths.at(".supervision-host-readback"))
            except OSError:
                readback = ""
            else:
                rc, _ = self.run([paths.script("fm-afk-contract.sh"), "readback"], stdout=fd,
                            env={**self.env, "FM_STATE_OVERRIDE": paths.state})
                if rc != 0:
                    os.ftruncate(fd, 0)
                os.close(fd)
        # The dialog mirror (bin/fm-host-mirror.sh) rides at the head of an
        # attended wake. The engine never judges without the captain's words,
        # so a feed that cannot be read hands the wake to main. Away needs
        # none, so an away wake never reads the mirror or moves its cursor.
        mirror = paths.mirror_feed
        files.remove(mirror)
        if self.turn_posture == "attended" and self.private_run(
                [paths.script("fm-host-mirror.sh"), "feed", self.engine_session, self.engine_mode], mirror) != 0:
            files.remove(paths.turn_file, mirror)
            self.grant("release", self.gen)
            self.handle_why = "the dialog mirror could not be read"
            return 1
        prompt_args = ["--report", "the bin/fm-branch-report.sh command"]
        if self.turn_posture == "away":
            prompt_args += ["--away", *(["--readback-file", readback] if readback else [])]
        else:
            prompt_args += ["--mirror-file", mirror]
        files.remove(paths.wake_file)
        rc = self.private_run(["node", paths.script("fm-branch-dispatch.mjs"), "wake-prompt", *prompt_args],
                          paths.wake_file, input_text=reason + "\n")
        if rc != 0:
            if readback:
                files.remove(readback)
            files.remove(paths.turn_file, mirror)
            self.grant("release", self.gen)
            self.handle_why = ("the dialog mirror could not be read" if rc == 3
                               else "the wake prompt could not be rendered")
            return 1
        if readback:
            files.remove(readback)
        files.remove(mirror)
        if self.turn_crosses_boundary():
            files.remove(paths.turn_file)
            self.grant("release", self.gen)
            self.boundary_exit()
        rc = self.run_engine_turn(turn)
        self.release_branch_leases()
        queued = self.call("fm_wake_rows_queued", *rows.split())
        unacked = text.unacknowledged(rows, queued.out if queued.ok else None)
        self.grant("release", self.gen)
        files.remove(paths.turn_file)
        receipts = sum(1 for line in files.lines(files.read_text(paths.receipts) or "")
                       if line.split("\t", 1)[0] == turn)
        usage = self.call("fm_supervision_engine_result", self.engine, self.turn_result, self.engine_cost or "0",
                          stderr=NULL).text if self.turn_result else ""
        files.remove(self.turn_result)
        self.turn_result = ""
        self.engine_error = rc != 0 or not usage.startswith("error=0")
        self.health_record(self.engine_error, receipts)
        errors = self.turn_errors
        if not self.engine_error and receipts > 0 and not unacked:
            cost = _usage_field(usage, "conversation_cost")
            if not self.write_engine_record(self.engine_turns + 1, cost):
                files.remove(paths.engine_record)
            if self.turn_posture == "attended":
                self.run([paths.script("fm-host-mirror.sh"), "commit"])
            files.remove(errors)
            self.turn_errors = ""
            self.log_line(f"handled\tturn={turn}\tposture={self.turn_posture}\trc={rc}\treports={receipts}\t{usage}"
                          f"\t{first}")
            return 0
        # A turn that did not handle its wake starts the next one on a new
        # conversation, so whatever went wrong in this one is not carried forward.
        files.remove(paths.engine_record)
        diagnostics = _diagnostics(errors)
        self.log_line(f"failed\tturn={turn}\tposture={self.turn_posture}\trc={rc}\treports={receipts}"
                      f"\tunacked={unacked or 'none'}\t{usage or 'no-result'}\t{diagnostics}\t{first}")
        files.remove(errors)
        self.turn_errors = ""
        timed_out = rc != 0 and self.call("fm_timed_out", rc, stdout=NULL).ok
        self.handle_why = text.turn_failure(rc, timed_out, usage, receipts, unacked, self.settings.turn_timeout,
                                            self.engine)
        return 1

    def run_engine_turn(self, turn):
        """One bounded engine turn, run in the background so a stop signal is
        handled at once; the cleanup stops the engine."""
        paths = self.paths
        with STOPS.shielded():
            self.turn_result = _mkstemp_or_empty(paths.at(".supervision-host-result"))
            self.turn_errors = _mkstemp_or_empty(paths.at(".supervision-host-errors"))
            env = {**self.engine_env(), "FM_SUPERVISION_ACTOR": "branch",
                   "FM_LEASE_HOLDER_PID": files.first_line(paths.at(".lock")),
                   "FM_SUPERVISION_PRIMARY_HARNESS": self.primary, "FM_BRANCH_REPORT_TURN": turn}
            self.engine_running = True
            self.engine_proc = self.bash.start(
                "fm_supervision_engine_turn", self.engine, self.engine_model, paths.prompt_file, paths.wake_file,
                self.engine_session, self.engine_mode, self.settings.turn_timeout, self.turn_result or os.devnull,
                self.turn_errors or os.devnull, paths.engine_pid_file, env=env)
        rc = self.engine_proc.wait()
        self.engine_proc = None
        self.engine_running = False
        return rc

    def engine_env(self):
        """An engine turn's environment. The turn's bound (fm_exec_timed) stops
        the engine once this host is gone, as it did when `$$` named the host."""
        return {**self.env, "FM_HOME": self.paths.home, "STATE": self.paths.state,
                "FM_EXEC_TIMED_OWNER_PID": self.pid}

    def turn_captain_seqs(self, turn):
        """The captain outcomes one turn recorded, as store rows."""
        found = [files.tsv(line, 4) for line in files.lines(files.read_text(self.paths.receipts) or "")]
        return ", ".join(row[1] for row in found if row[0] == turn and row[2] == "captain")

    def returned_during_turn(self):
        """True when the captain returned during this close's engine turn and that
        turn recorded visible outcomes. A lookup failure is distinct from a valid
        turn with no visible outcomes."""
        self.turn_receipt_seqs = ""
        self.returned_rows = ""
        self.returned_seqs = ""
        self.returned_lookup_failed = False
        if not self.last_turn or self.turn_posture != "away" or self.away_present():
            return False
        receipts = files.read_text(self.paths.receipts)
        if receipts is None:
            self.returned_lookup_failed = True
            return False
        self.turn_receipt_seqs = ",".join(files.tsv(line, 3)[1] for line in files.lines(receipts)
                                          if line.split("\t", 1)[0] == self.last_turn)
        if not self.turn_receipt_seqs:
            return False
        rc, rows = self.run([self.paths.script("fm-branch-outcome.sh"), "lookup", "--seqs", self.turn_receipt_seqs],
                       stdout=CAPTURE)
        if rc != 0:
            self.returned_lookup_failed = True
            return False
        self.returned_rows = files.output(rows)
        try:
            self.returned_seqs = text.visible_seqs(self.returned_rows)
        except ValueError:
            self.returned_lookup_failed = True
            return False
        return bool(self.returned_seqs)

    def outcome_lookup_warning(self):
        return (f"supervision-host: outcome lookup failed for turn receipt rows {self.turn_receipt_seqs or 'unknown'};"
                " visible outcomes may require manual review")

    def with_health_note(self, line):
        return line + ("\n" + self.health_note if self.health_note else "")

    # --- the park -------------------------------------------------------------

    def park(self):
        # Ownership first: a host that does not own supervision leaves the
        # owner's host, processes, arms, and leases alone.
        if not self.host_still_owner():
            self.stand_down("this session does not own supervision")
        STOPS.install()
        self.cleanup_armed = True
        if not self.activate():
            write_out("supervision-host stood down: the host record could not be written\n")
            raise Exit(0)
        self.log_line(f"start\tgen={self.gen}\tprimary={self.primary}")
        if self.restart:
            self.start_arm("arm", self.owner_predecessor, "--restart")
        elif self.left_arm:
            self.log_line(f"take-over\tarm={self.left_arm}")
            self.start_arm("arm", self.owner_predecessor, "--take-over", self.left_arm)
        else:
            self.start_arm("arm", self.owner_predecessor)
        if self.arm is None:
            write_out("watcher: FAILED - the supervision host could not start a watcher cycle\n")
            raise Exit(1)
        while True:
            self.one_close()

    def one_close(self):
        if self.boundary_reached():
            self.boundary_exit()
        if not self.await_close():
            self.boundary_exit()
        self.reason = text.wake_lines(self.arm_text)
        first = text.first(self.reason)
        # The away daemon owns triage while its flag exists; the owner stands
        # down. A close with no wake is the arm's own failure or attach result,
        # which the owner judges exactly as it judges the arm's. Exit status 0
        # in both: a status above 128 tells the owner the host itself died.
        if not self.reason:
            self.log_line("pass-through\ta close without a wake")
            self.emit()
            raise Exit(0)
        if os.path.exists(self.paths.at(".afk")):
            self.log_line(f"pass-through\tthe away daemon's flag exists\t{first}")
            self.emit()
            raise Exit(0)
        if not self.away_present():
            # Attended: the close reaches main exactly as the plain arm delivers
            # it, unless the supervision session may take it.
            if not self.attended_acceptor(first):
                self.log_line(f"pass-through\tattended\t{self.attended_why}\t{first}")
                self.acknowledged_handback()
                if self.attended_why == "main-only" and self.leave_successor_for_main() == 2:
                    # An actionable banner cannot commit while the marker is handling.
                    write_out("watcher: FAILED - the main-only hand-back could not restore watcher downtime\n")
                    raise Exit(1)
                self.emit()
                raise Exit(0)
            if not self.host_still_owner():
                self.stand_down(NO_LONGER_OWNER)
        else:
            if not self.host_still_owner():
                self.stand_down(NO_LONGER_OWNER)
            if not self.host_config():
                self.exit_to_main("the home no longer runs the supervision host")
            if not self.engine:
                self.exit_to_main(f"no supervision engine runs here: {self.engine_problem}; this wake is yours")
            if not _on_path("node", self.env):
                self.exit_to_main("node is required to compute branch eligibility; this wake is yours")
            if self.health_cooling():
                clock = self.call("fm_supervision_host_clock", self.health.retry).text
                self.exit_to_main(f"the away session is paused after repeated engine errors until {clock}; "
                                  "this wake is yours")
        # A turn that could outlive the boundary would outlive the hook registration.
        if self.turn_crosses_boundary():
            self.boundary_exit()
        if not self.start_successor(self.closed_arm_pid):
            self.exit_to_main("the successor watcher cycle could not be verified before handling; this wake is yours")
        if self.successor_generation and self.run(
                [self.paths.script("fm-watch-arm.sh"), "--handling-delivered", self.successor_generation,
                 "--watcher-pid", self.successor_watcher])[0] != 0:
            self.exit_to_main("the handling handoff to the successor watcher could not be confirmed; "
                              "this wake is yours")
        handled = self.handle_wake(self.reason)
        if handled == 2:
            self.log_line(f"pass-through\tattended\t{self.attended_why}\t{first}")
            self.acknowledged_handback()
            # The successor this turn already started and confirmed stays up.
            # Retiring it is what left no watcher after a close that became
            # main-only.
            self.detach_successor()
            # Main handles this close after all, so hand back the downtime the
            # handoff above consumed: the re-arm owner delivers the close only
            # while the recovery marker reads downtime. It is handed back as
            # already announced, so the successor left running does not
            # resurface the episode this very close delivers.
            if self.successor_generation and not self.call(
                    "fm_recovery_marker_handback", self.paths.marker, stdout=NULL, stderr=NULL).ok:
                self.log_line(f"pass-through\tdowntime-unrestored\t{first}")
                raise Exit(1)
            self.emit()
            raise Exit(0)
        # The captain returned during that turn: the return brief was rendered
        # before its visible outcomes existed, so main relays them now, handled
        # or not.
        if handled != 0:
            if self.returned_during_turn():
                self.exit_to_main(
                    f"the away session could not take this wake: {self.handle_why}; this wake is yours, and the "
                    f"captain returned during its turn, so relay the visible outcomes it recorded (store rows "
                    f"{self.returned_seqs}, listed next and in bin/fm-branch-outcome.sh list) to the captain",
                    self.with_health_note(text.outcome_lines(self.returned_rows)))
            elif self.returned_lookup_failed:
                self.exit_to_main(
                    f"the away session could not take this wake: {self.handle_why}; this wake is yours, and the "
                    "captain returned during its turn, but the recorded outcomes could not be verified",
                    self.with_health_note(self.outcome_lookup_warning()))
            if self.turn_posture == "away":
                self.exit_to_main(f"the away session could not take this wake: {self.handle_why}; this wake is yours",
                                  self.health_note)
            self.exit_to_main(f"the supervision session could not take this wake: {self.handle_why}; "
                              "this wake is yours", self.health_note)
        if self.returned_during_turn():
            self.exit_to_main(
                "the captain returned while the away session was handling this wake, which it finished after the "
                f"return brief was rendered; relay its visible outcomes (store rows {self.returned_seqs}, listed "
                "next and in bin/fm-branch-outcome.sh list) to the captain",
                text.outcome_lines(self.returned_rows))
        elif self.returned_lookup_failed:
            self.exit_to_main(
                "the captain returned while the away session was handling this wake, but the recorded outcomes "
                "could not be verified; main must review them", self.outcome_lookup_warning())
        # Attended captain outcomes are main's to process; away they wait for
        # the return, including when the captain left while this turn ran. The
        # close itself was handled, so only the host's lines reach main.
        if self.last_turn and not self.away_present():
            captain = self.turn_captain_seqs(self.last_turn)
            if captain:
                self.arm_text = ""
                self.exit_to_main(
                    f"branch-outcome: the supervision session handled this wake and recorded captain outcomes for "
                    f"you (store rows {captain}); run bin/fm-wake-drain.sh, act on its BRANCH OUTCOMES section, "
                    "and acknowledge them as it prints", self.health_note)
        # Handled: park on the successor.
        if not self.call("fm_recovery_marker_handling_settled", self.paths.marker, stdout=INHERIT).ok:
            self.log_line(f"handling-settle-failed\tturn={self.last_turn}\t"
                          f"successor={self.successor.pid if self.successor else ''}")
        self.arm = self.successor
        self.successor = None
        self.arm_text = ""


def _scope_field(scope, name):
    """A `name=value` line of a dispatch scope or offer: every such line's value, joined."""
    prefix = name + "="
    return "\n".join(line[len(prefix):] for line in scope.split("\n") if line.startswith(prefix))


def _usage_field(usage, name):
    marker = f" {name}="
    if marker not in usage:
        return ""
    return usage.rsplit(marker, 1)[1].split(" ", 1)[0]


def _diagnostics(path):
    """The first 300 bytes of a turn's diagnostics on one line."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(300)
    except (OSError, TypeError):
        return ""
    return files.decode(data).replace("\t", " ").replace("\n", " ")


def _write(path, content):
    try:
        with open(path, "wb") as handle:
            handle.write(files.encode(content))
    except OSError:
        pass


def _mkstemp_or_empty(prefix):
    try:
        fd, path = files.mkstemp(prefix)
    except OSError:
        return ""
    os.close(fd)
    return path



def _on_path(name, env):
    return shutil.which(name, path=env.get("PATH")) is not None
