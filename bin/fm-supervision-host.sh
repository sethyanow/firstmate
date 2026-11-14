#!/usr/bin/env bash
# fm-supervision-host.sh - the supervision host (docs/supervision-host.md owns
# the design).
#
# Usage:
#   fm-supervision-host.sh park [--restart]
#   fm-supervision-host.sh --help
#
# The host is the `fm host` command of this repository's Python package
# (python/, run through uv). python/src/firstmate/host/__init__.py holds its
# contract, which --help prints. This shim resolves the package's interpreter
# with `uv run` and replaces itself with the command, so the host keeps the
# pid, environment, standard streams, and exit status its owner sees.
# FM_HOST_PID names that pid to the host.
#
# On native Windows (Git Bash), the interpreter is a Windows process: the
# owner's signals cannot reach it, and the Git Bash pid the owner and the bash
# libraries know is this shim's. So the shim stays, runs the host with its
# standard input as a stop relay (FM_HOST_STOP_RELAY), passes each TERM, HUP,
# or INT on as its number, and exits with the host's status. The relay closes
# when the shim dies, which also stops the host.
#
# mise.toml at the repository root pins uv. Where mise is installed, uv runs
# through mise against that file, so the pin holds whatever the caller's
# working directory. Elsewhere the uv on PATH runs. --no-dev leaves out the
# lint tools.
set -u
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || exit
bin_dir=$(cd "$script_dir" && pwd -P) || exit
root=${bin_dir%/*}
uv=(uv)
if command -v mise >/dev/null 2>&1; then
  uv=(mise --quiet --cd "$root" exec -- uv)
fi
python=$("${uv[@]}" run --quiet --frozen --no-dev --project "$root/python" \
  python -I -c 'import sys; print(sys.executable)') || exit
export FM_HOST_SCRIPT_DIR=$script_dir FM_HOST_PID=$$
case ${OSTYPE:-} in
  msys* | cygwin*) ;;
  *) exec "$python" -I -m firstmate host "$@" ;;
esac
export FM_HOST_STOP_RELAY=1
# Process substitution, because stock macOS Bash 3.2 must still parse this
# file and has no coproc. A Git Bash named pipe is no handle to a Windows
# program.
exec 4> >(exec "$python" -I -m firstmate host "$@")
host=$!
# shellcheck disable=SC2329 # the traps below invoke it
relay() { { printf '%s\n' "$1" 1>&4; } 2>/dev/null || true; }
trap 'relay 15' TERM
trap 'relay 1' HUP
trap 'relay 2' INT
# A relayed signal interrupts the wait while the host is still cleaning up.
# Once the host is gone, wait gives its own status.
while kill -0 "$host" 2>/dev/null; do
  wait "$host"
done
wait "$host"
