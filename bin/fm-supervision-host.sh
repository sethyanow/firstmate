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
FM_HOST_SCRIPT_DIR=$script_dir exec "$python" -I -m firstmate host "$@"
