#!/usr/bin/env bash
# fm-python-lib.sh - shared helpers for running this repo's Python tooling.
#
# Sourced, never executed.
#
#   fm_repo_uv <root> <uv args...>
#       Runs uv with the given arguments. When mise is installed, uv comes
#       from mise in <root>, so the repo's pinned uv version applies.

fm_repo_uv() {  # <root> <uv args...>
  local root=$1
  shift
  if command -v mise >/dev/null 2>&1; then
    mise --quiet --cd "$root" exec -- uv "$@"
  else
    uv "$@"
  fi
}
