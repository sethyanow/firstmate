#!/usr/bin/env bash
# fm-supervision-host.sh - the supervision host (docs/supervision-host.md owns
# the design).
#
# Usage:
#   fm-supervision-host.sh park [--restart]
#   fm-supervision-host.sh --help
#
# The host is the `fm host` command of this repository's Python package
# (python/, run through uv); python/src/firstmate/host/__init__.py holds its
# contract, which --help prints. This shim resolves the package's interpreter
# with `uv run` and replaces itself with the command, so the host keeps the
# pid, environment, standard streams, and exit status its owner sees.
#
# --no-config keeps uv from reading [tool.uv] in python/pyproject.toml. Its
# required-version pins uv for lint and tests only, so a uv update cannot stop
# the host. --no-dev leaves out the lint tools.
set -u
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || exit
project="$(cd "$script_dir" && pwd -P)/../python"
python=$(uv run --quiet --no-config --frozen --no-dev --project "$project" \
  python -I -c 'import sys; print(sys.executable)') || exit
FM_HOST_SCRIPT_DIR=$script_dir exec "$python" -I -m firstmate host "$@"
