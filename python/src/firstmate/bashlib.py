"""The one adapter from this package to the bash libraries under bin/.

A converted script reaches a library function the bash scripts share through
Bash, so each shared concern keeps one implementation, in bash.
Every call runs a fresh bash that sets the script's globals as shell variables,
sources the script's libraries in the script's order under `set -u`, the state
the script's own functions ran in, and runs the function with its arguments.
A call runs in the foreground, shielded from stop signals (firstmate.signals).
Output variables a function sets come back through a private file.
"""

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from . import files
from .processes import CAPTURE, INHERIT, NULL, Stream, exit_status, finish
from .signals import Stops

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
class Bash:
    """Library calls for one script: its bin directory, libraries, globals, and environment."""

    script_dir: str
    libraries: Sequence[str]
    arg0: str
    shell_globals: Mapping[str, str]
    env: Mapping[str, str]
    stops: Stops

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
            "bash",
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
        """Run `func args...` to completion; its output when captured, and `out_vars`.
        A call that cannot start fails as a command that cannot run does."""
        vars_file = ""
        try:
            if out_vars:
                fd, vars_file = tempfile.mkstemp(prefix="fm-bashlib-vars.")
                os.close(fd)
            argv = self.argv(func, args, out_vars, vars_file)
            with self.stops.shielded():
                proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, env=self.env)
                out = finish(proc, self.stops)
            values = (files.read_text(vars_file) or "").split("\0") if vars_file else []
            return Result(
                exit_status(proc.returncode),
                files.decode(out) if out is not None else "",
                _named(out_vars, values) if out_vars else {},
            )
        except OSError as error:
            status = 127 if isinstance(error, FileNotFoundError) else 126
            return Result(status, "", dict.fromkeys(out_vars, ""))
        finally:
            files.remove(vars_file)

    def start(
        self,
        func: str,
        *args: object,
        stdout: Stream = INHERIT,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start `func args...` in the background; the caller owns the process."""
        return subprocess.Popen(
            self.argv(func, args), stdout=stdout, env=self.env if env is None else env
        )

    def value(self, name: str) -> str:
        """A library global's value after sourcing."""
        return self.call("true", stdout=NULL, out_vars=(name,)).values[name]


def _named(names: Sequence[str], values: Sequence[str]) -> dict[str, str]:
    return {name: values[i] if i < len(values) else "" for i, name in enumerate(names)}
