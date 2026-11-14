"""Processes a converted script checks, signals, and runs in the foreground."""

import enum
import os
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
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


# A child's standard error joined to its standard output.
TO_STDOUT = subprocess.STDOUT


@dataclass(frozen=True)
class Streams:
    """A child's standard input, output, and error."""

    stdin: Stream = INHERIT
    stdout: Stream = INHERIT
    stderr: Stream = INHERIT


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


class Sig(enum.Enum):
    """A signal the host sends, by the name `kill -s` takes."""

    TERM = "TERM"
    KILL = "KILL"


def pid_alive(pid: int | None) -> bool:
    """True while a process answers to `pid` (`kill -0`), or exists but may
    not be signalled by this user. POSIX only: on Windows os.kill ends the
    process, so firstmate.bashlib asks Git Bash instead."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def send(pid: int, sig: Sig) -> bool:
    """Send `sig`. False when no process took it. POSIX only, as pid_alive."""
    try:
        os.kill(pid, signal.Signals[f"SIG{sig.value}"])
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


Terminate = Callable[[subprocess.Popen[bytes]], None]


def finish(
    proc: subprocess.Popen[bytes],
    stops: Stops,
    data: bytes | None = None,
    terminate: Terminate = subprocess.Popen.terminate,
) -> bytes | None:
    """Feed and wait for a foreground child, and return its captured output.

    Call it inside stops.shielded(). A child still running once a stop has
    waited its grace is ended: `terminate` (TERM), then KILL a second later.
    Output a descendant still holds open a second after that is not waited for.
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
        terminate(proc)
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


def run(
    command: Command, stops: Stops, terminate: Terminate = subprocess.Popen.terminate
) -> Completed:
    """Run a command to completion in the foreground, shielded from a stop, which
    ends it through `terminate` (finish). A command that cannot start fails as a
    shell reports it: 127 or 126. Its standard error is discarded."""
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
        out = finish(proc, stops, data, terminate)
    return Completed(exit_status(proc.returncode), files.decode(out) if out else "")
