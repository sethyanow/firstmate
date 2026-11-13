"""Processes a converted script checks, signals, and runs in the foreground."""

import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import files
from .signals import Stops

_POLL_SECONDS = 0.1
_KILL_AFTER_SECONDS = 1.0

NULL = subprocess.DEVNULL
CAPTURE = subprocess.PIPE
# The process's own stream, as a child started without a redirection gets it.
INHERIT = None
Stream = int | None


def pid_from(text: str) -> int | None:
    """A positive pid written as plain ASCII digits, or None."""
    if not text.isascii() or not text.isdigit():
        return None
    pid = int(text)
    return pid if pid > 0 else None


def start_failure_status(error: OSError) -> int:
    """The status a shell reports for a command it cannot start: 127 when the
    command is missing, 126 otherwise."""
    return 127 if isinstance(error, FileNotFoundError) else 126


if sys.platform == "win32":
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _NOT_INHERITED = 0
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    def pid_alive(pid: int | None) -> bool:
        """True while the process `pid` has no exit code yet, or exists but
        may not be queried.

        Windows has no `kill -0`: os.kill there ends the process for any
        signal but the console events.
        """
        if pid is None:
            return False
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, _NOT_INHERITED, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and (
                code.value == _STILL_ACTIVE
            )
        finally:
            kernel32.CloseHandle(handle)

else:

    def pid_alive(pid: int | None) -> bool:
        """True while a process answers to `pid` (`kill -0`), or exists but
        may not be signalled by this user."""
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True


def send(pid: int, sig: signal.Signals) -> bool:
    """Send `sig`. False when no process took it."""
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


def exit_status(returncode: int) -> int:
    """A child's exit status as a shell reports it: 128 plus the signal number
    for a child a signal ended, where subprocess gives the negative number."""
    return 128 - returncode if returncode < 0 else returncode


@dataclass(frozen=True)
class Completed:
    """A foreground command's exit status and its captured output."""

    rc: int
    out: str = ""


def finish(proc: subprocess.Popen[bytes], stops: Stops, data: bytes | None = None) -> bytes | None:
    """Feed and wait for a foreground child, and return its captured output.

    Call it inside stops.shielded(). A child still running once a stop has
    waited its grace is ended: TERM, then KILL a second later. Output a
    descendant still holds open a second after that is not waited for.
    """
    try:
        while True:
            try:
                out, _ = proc.communicate(data, timeout=_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                data = None
                if stops.overdue():
                    break
            else:
                return out
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=_KILL_AFTER_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, _ = proc.communicate(timeout=_KILL_AFTER_SECONDS)
            except subprocess.TimeoutExpired:
                out = None
        return out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                pipe.close()


@dataclass(frozen=True)
class Command:
    """A command to run in the foreground: its arguments, environment, standard
    output, standard input text, and the umask its files are created under."""

    argv: Sequence[str]
    env: Mapping[str, str]
    stdout: Stream = NULL
    input_text: str | None = None
    umask: int = -1


def run(command: Command, stops: Stops) -> Completed:
    """Run a command to completion in the foreground, shielded from a stop.
    A command that cannot start fails as a shell reports it: 127 or 126.
    Its standard error is discarded."""
    data = None if command.input_text is None else files.encode(command.input_text)
    with stops.shielded():
        try:
            proc = subprocess.Popen(
                command.argv,
                stdin=subprocess.PIPE if data is not None else None,
                stdout=command.stdout,
                stderr=NULL,
                env=command.env,
                umask=command.umask,
            )
        except OSError as error:
            return Completed(start_failure_status(error))
        out = finish(proc, stops, data)
    return Completed(exit_status(proc.returncode), files.decode(out) if out else "")
