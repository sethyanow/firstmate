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
from dataclasses import dataclass, field

from . import files
from .signals import STOPS, exit_status

CAPTURE = subprocess.PIPE
NULL = subprocess.DEVNULL
INHERIT = None

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


@dataclass
class Result:
    rc: int
    out: str = ""
    vars: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.rc == 0

    @property
    def text(self):
        """The output as `$(...)` gives it."""
        return files.output(self.out)


class Bash:
    """Library calls for one script: its bin directory, libraries, globals, and environment."""

    def __init__(self, script_dir, libraries, arg0, shell_globals, env):
        self.env = env
        self.script_dir = script_dir
        self.libraries = " ".join(libraries)
        self.arg0 = arg0
        self.globals = dict(shell_globals)

    def argv(self, func, args, var_names=(), vars_file="", libraries=None):
        assigns = [f"{name}={value}" for name, value in self.globals.items()]
        libraries = self.libraries if libraries is None else " ".join(libraries)
        return ["bash", "-c", _ADAPTER, self.arg0, *assigns, "--", self.script_dir, libraries,
                " ".join(var_names), vars_file, func, *(str(arg) for arg in args)]

    def call(self, func, *args, stdout=CAPTURE, stderr=INHERIT, env=None, out_vars=(), libraries=None):
        """Run `func args...` to completion; its output when captured, and `out_vars`.
        `libraries` narrows the sourced set for a hot call whose function needs fewer.
        A call that cannot start fails as a command that cannot run does."""
        vars_file = ""
        try:
            try:
                if out_vars:
                    fd, vars_file = tempfile.mkstemp(prefix="fm-bashlib-vars.")
                    os.close(fd)
                with STOPS.shielded():
                    proc = subprocess.Popen(self.argv(func, args, out_vars, vars_file, libraries), stdout=stdout,
                                            stderr=stderr, env=self.env if env is None else env)
                    out, _ = proc.communicate()
            except OSError as error:
                return Result(127 if isinstance(error, FileNotFoundError) else 126, "", dict.fromkeys(out_vars, ""))
            values = {}
            if out_vars:
                raw = (files.read_text(vars_file) or "").split("\0")
                values = {name: raw[i] if i < len(raw) else "" for i, name in enumerate(out_vars)}
            return Result(exit_status(proc.returncode), files.decode(out) if out is not None else "", values)
        finally:
            if vars_file:
                files.remove(vars_file)

    def start(self, func, *args, stdout=INHERIT, stderr=INHERIT, env=None):
        """Start `func args...` in the background; the caller owns the process."""
        return subprocess.Popen(self.argv(func, args), stdout=stdout, stderr=stderr,
                                env=self.env if env is None else env)

    def value(self, name):
        """A library global's value after sourcing."""
        return self.call("true", stdout=NULL, out_vars=(name,)).vars[name]
