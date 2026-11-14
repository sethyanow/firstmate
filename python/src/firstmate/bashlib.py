"""The one adapter from this package to the bash libraries under bin/.

A converted script reaches a library function the bash scripts share through
Bash, so each shared concern keeps one implementation, in bash.
Every call runs a fresh bash that sets the script's globals as shell variables,
sources the script's libraries in the script's order under `set -u`, the state
the script's own functions ran in, and runs the function with its arguments.
A call runs in the foreground, shielded from stop signals (firstmate.signals).
Output variables a function sets come back through a private file.

A pid this package records, compares, or hands a bash script is the pid `$$`
reports in bash. On native Windows that is a Git Bash pid, which neither
Python nor Windows knows, so a process the host starts is learned, checked,
and signalled through Git Bash (Bash.spawn, Bash.alive, Bash.send).
"""

import functools
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from . import files, processes
from .processes import (
    CAPTURE,
    INHERIT,
    NULL,
    Sig,
    Stream,
    Streams,
    exit_status,
    finish,
    start_failure_status,
)
from .signals import Stops

WINDOWS = sys.platform == "win32"
_KILL_SECONDS = 10
_PID_POLL_SECONDS = 0.02
# Git Bash starts slowly on a loaded host (bin/fm-watch-arm.sh allows 30s there).
_PID_REPORT_SECONDS = 30

# Writes the shell's pid, then becomes the command: exec keeps the Git Bash pid.
_REPORT_PID = 'printf "%s\\n" "$$" > "$1"; shift; exec "$@"'

_ADAPTER = r"""set -u
while [ "$1" != -- ]; do
  printf -v "${1%%=*}" '%s' "${1#*=}"
  shift
done
shift
_fm_py_dir=$1 _fm_py_libs=$2 _fm_py_vars=$3 _fm_py_vars_file=$4
shift 4
for _fm_py_lib in $_fm_py_libs; do
  . "$_fm_py_dir/$_fm_py_lib"
done
"$@"
_fm_py_rc=$?
if [ -n "$_fm_py_vars_file" ]; then
  for _fm_py_var in $_fm_py_vars; do
    printf '%s\0' "${!_fm_py_var-}"
  done > "$_fm_py_vars_file"
fi
exit "$_fm_py_rc"
"""


@dataclass(frozen=True)
class Result:
    """A finished library call: its status, captured output, and output variables."""

    rc: int
    out: str = ""
    values: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def text(self) -> str:
        """The output as `$(...)` gives it."""
        return files.output(self.out)


@dataclass(frozen=True)
class Started:
    """A process started in the background, and the pid bash knows it by."""

    proc: subprocess.Popen[bytes]
    pid: int


@dataclass(frozen=True)
class Bash:
    """Library calls for one script: its bin directory, libraries, globals, and environment."""

    script_dir: str
    libraries: Sequence[str]
    arg0: str
    shell_globals: Mapping[str, str]
    env: Mapping[str, str]
    stops: Stops

    @functools.cached_property
    def executable(self) -> str:
        """The bash the calls and the bin scripts run under: the first on this
        environment's PATH. Windows searches its system folder, where WSL's
        bash.exe lives, before PATH."""
        return shutil.which("bash", path=self.env.get("PATH", os.defpath)) or "bash"

    def sourcing(self, libraries: Sequence[str]) -> "Bash":
        """These calls with fewer libraries, for a hot call whose function needs fewer."""
        return replace(self, libraries=tuple(libraries))

    def with_globals(self, extra: Mapping[str, str]) -> "Bash":
        """These calls with further globals set."""
        return replace(self, shell_globals={**self.shell_globals, **extra})

    def argv(
        self, func: str, args: Sequence[object], var_names: Sequence[str] = (), vars_file: str = ""
    ) -> list[str]:
        assigns = [f"{name}={value}" for name, value in self.shell_globals.items()]
        return [
            self.executable,
            "-c",
            _ADAPTER,
            self.arg0,
            *assigns,
            "--",
            self.script_dir,
            " ".join(self.libraries),
            " ".join(var_names),
            vars_file,
            func,
            *(str(arg) for arg in args),
        ]

    def call(
        self,
        func: str,
        *args: object,
        stdout: Stream = CAPTURE,
        stderr: Stream = INHERIT,
        out_vars: Sequence[str] = (),
    ) -> Result:
        """Run `func args...` to completion. The result holds its output when
        captured, and `out_vars`. A call that cannot start fails as a command
        that cannot run does."""
        vars_file = ""
        try:
            if out_vars:
                fd, vars_file = tempfile.mkstemp(prefix="fm-bashlib-vars.")
                os.close(fd)
            argv = self.argv(func, args, out_vars, vars_file)
            with self.stops.shielded():
                proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, env=self.env)
                out = finish(proc, self.stops, terminate=self.terminate)
            values = (files.read_text(vars_file) or "").split("\0") if vars_file else []
            return Result(
                exit_status(proc.returncode),
                files.decode(out) if out is not None else "",
                _named(out_vars, values) if out_vars else {},
            )
        except OSError as error:
            return Result(start_failure_status(error), "", dict.fromkeys(out_vars, ""))
        finally:
            files.remove(vars_file)

    def start(
        self,
        func: str,
        *args: object,
        stdout: Stream = INHERIT,
        env: Mapping[str, str] | None = None,
    ) -> Started:
        """Start `func args...` in the background. The caller owns the process."""
        return self.spawn(
            self.argv(func, args), Streams(stdout=stdout), env=self.env if env is None else env
        )

    def spawn(
        self,
        argv: Sequence[str],
        streams: Streams,
        *,
        env: Mapping[str, str] | None = None,
        own_group: bool = False,
    ) -> Started:
        """Start `argv` in the background, in a process group of its own with
        `own_group`. Raises OSError when it cannot start.

        On Windows the command starts behind a bash that reports its Git Bash
        pid. A Git Bash process a Windows process starts already leads a group
        of its own.
        """
        env = self.env if env is None else env
        if not WINDOWS:
            proc = subprocess.Popen(
                argv,
                stdin=streams.stdin,
                stdout=streams.stdout,
                stderr=streams.stderr,
                env=env,
                process_group=0 if own_group else None,
            )
            return Started(proc, proc.pid)
        fd, pid_file = tempfile.mkstemp(prefix="fm-bashlib-pid.")
        os.close(fd)
        try:
            proc = subprocess.Popen(
                [self.executable, "-c", _REPORT_PID, "fm-pid", pid_file, *argv],
                stdin=streams.stdin,
                stdout=streams.stdout,
                stderr=streams.stderr,
                env=env,
            )
            pid = _reported_pid(proc, pid_file)
        finally:
            files.remove(pid_file)
        if pid is None:
            proc.kill()
            proc.wait()
            raise OSError(f"{argv[0]} did not report its Git Bash pid")
        return Started(proc, pid)

    def alive(self, pid: int | None) -> bool:
        """True while the process bash knows as `pid` runs (`kill -0`)."""
        if pid is None:
            return False
        if not WINDOWS:
            return processes.pid_alive(pid)
        # A probe that cannot run says nothing, so the process may still run.
        return self._kill("kill", "-0", pid) in (0, None)

    def send(self, pid: int, sig: Sig) -> bool:
        """Send `sig` to the process bash knows as `pid`. False when no process
        took it. A Git Bash process runs its trap, as on POSIX."""
        if not WINDOWS:
            return processes.send(pid, sig)
        return self._kill("kill", "-s", sig.value, pid) == 0

    def terminate(self, proc: subprocess.Popen[bytes]) -> None:
        """TERM a foreground child. On Windows, Popen.terminate ends the process
        at once, so a Git Bash child gets a Git Bash TERM by its Windows pid
        instead, and any other child is ended."""
        if not WINDOWS or self._kill("/usr/bin/kill", "-W", "-s", "TERM", proc.pid) != 0:
            proc.terminate()

    def _kill(self, kill: str, *args: object) -> int | None:
        """Run `kill args...` in bash, outside the stop handling: a stop that
        is already overdue must not end it. Its status, or None when it could
        not run."""
        try:
            return subprocess.run(
                # Git Bash splits an argument with a quote and no space wrongly.
                [self.executable, "-c", f'{kill} "$@"', "kill", *(str(arg) for arg in args)],
                stdin=NULL,
                stdout=NULL,
                stderr=NULL,
                env=self.env,
                timeout=_KILL_SECONDS,
                check=False,
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            return None

    def value(self, name: str) -> str:
        """A library global's value after sourcing."""
        return self.call("true", stdout=NULL, out_vars=(name,)).values[name]


def _reported_pid(proc: subprocess.Popen[bytes], pid_file: str) -> int | None:
    """The pid the child writes to `pid_file`, or None when it exits or takes
    too long first."""
    deadline = time.monotonic() + _PID_REPORT_SECONDS
    while time.monotonic() < deadline:
        # An exited child may have reported since the previous read.
        exited = proc.poll() is not None
        text = files.read_text(pid_file) or ""
        # Only a whole line is the whole pid.
        if text.endswith("\n"):
            return processes.pid_from(files.output(text))
        if exited:
            return None
        time.sleep(_PID_POLL_SECONDS)
    return None


def _named(names: Sequence[str], values: Sequence[str]) -> dict[str, str]:
    return {name: values[i] if i < len(values) else "" for i, name in enumerate(names)}
