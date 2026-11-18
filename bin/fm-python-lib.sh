#!/usr/bin/env bash

fm_repo_uv() {  # <root> <uv args...>
  local root=$1
  shift
  if command -v mise >/dev/null 2>&1; then
    mise --quiet --cd "$root" exec -- uv "$@"
  else
    uv "$@"
  fi
}
