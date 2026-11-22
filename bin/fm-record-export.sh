#!/usr/bin/env bash
# fm-record-export.sh - publish private fleet records and scout reports.
#
# Usage:
#   fm-record-export.sh sync
#   fm-record-export.sh archive-task <task-id>
#   fm-record-export.sh publish-report <task-id>
#   fm-record-export.sh import-reports
#   fm-record-export.sh index-reports
#
# Record commands use config/record-export-root. Report commands use
# config/report-collection-root. Each contains one absolute directory path.
# Automatic publication is inert without its config. Import and index require it.
# The Python engine owns the dedicated root, descriptor-safe traversal, exact
# live snapshots, durable transactions, manifests, and retention.
# Record commands run under python3. Report commands parse Markdown with the
# parser that python/'s reports dependency group locks, so they run through uv,
# and through mise where it is installed because mise.toml pins uv.
# Each report package's catalogue.v1.json holds: id (<home>/<folder>), title,
# summary, project, origin_home, origin_path, task_id, revision, legacy,
# authored, completed, captured_at (UTC), source (path under data/),
# source_sha256, files (package path to SHA-256), uncaptured_links, and capture
# (complete or partial). Unknown text values are "unknown".
# Task-specific calls hold the task metadata lock through publication.
# Lifecycle callers that already hold it set FM_RECORD_EXPORT_META_LOCK_HELD.
set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
FM_ROOT=${FM_ROOT_OVERRIDE:-$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd -P)}
FM_HOME=${FM_HOME:-$FM_ROOT}
CONFIG=${FM_CONFIG_OVERRIDE:-$FM_HOME/config}
STATE=${FM_STATE_OVERRIDE:-$FM_HOME/state}
EXPORT_META_LOCK=

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

record_export_configured() {  # <config-name>
  local config_file="$CONFIG/$1"
  if [ ! -e "$CONFIG" ] && [ ! -L "$CONFIG" ]; then
    return 1
  fi
  [ -d "$CONFIG" ] && [ ! -L "$CONFIG" ] \
    || die "record export config directory is unsafe: $CONFIG"
  [ -r "$CONFIG" ] && [ -x "$CONFIG" ] \
    || die "record export config directory is inaccessible: $CONFIG"
  if [ ! -e "$config_file" ] && [ ! -L "$config_file" ]; then
    return 1
  fi
  [ -f "$config_file" ] && [ ! -L "$config_file" ] && [ -r "$config_file" ] \
    || die "record export config is not a readable regular file: $config_file"
}

run_engine() {  # <command> [task-id]
  local -a uv
  case "$1" in
    sync|archive-task)
      command -v python3 >/dev/null 2>&1 || die "configured record export requires python3"
      python3 "$SCRIPT_DIR/fm-record-export.py" "$@"
      return
      ;;
  esac
  if command -v mise >/dev/null 2>&1; then
    uv=(mise --quiet --cd "$SCRIPT_DIR/.." exec -- uv)
  elif command -v uv >/dev/null 2>&1; then
    uv=(uv)
  else
    die "configured report collection requires uv"
  fi
  "${uv[@]}" run --quiet --frozen --only-group reports --project "$SCRIPT_DIR/../python" \
    python -I "$SCRIPT_DIR/fm-record-export.py" "$@"
}

export_lock_release() {
  [ -z "$EXPORT_META_LOCK" ] || fm_lock_release "$EXPORT_META_LOCK" || true
}

run_task_export() {  # <command> <task-id> <config-name>
  local command=$1 task=$2 rc=0
  record_export_configured "$3" || return 0
  if [ "${FM_RECORD_EXPORT_META_LOCK_HELD:-0}" = 1 ]; then
    run_engine "$command" "$task"
    return $?
  fi
  # shellcheck source=bin/fm-wake-lib.sh
  . "$SCRIPT_DIR/fm-wake-lib.sh"
  EXPORT_META_LOCK=$(fm_meta_lock_path "$STATE/$task.meta") \
    || die "invalid task id for $command: $task"
  fm_lock_acquire_wait "$EXPORT_META_LOCK" \
    || die "cannot acquire task lock for $command: $task"
  trap 'export_lock_release' EXIT HUP INT TERM
  run_engine "$command" "$task" || rc=$?
  export_lock_release
  EXPORT_META_LOCK=
  trap - EXIT HUP INT TERM
  return "$rc"
}

[ "$#" -ge 1 ] || usage
command=$1
shift
case "$command:$#" in
  sync:0)
    record_export_configured record-export-root || exit 0
    run_engine sync
    ;;
  archive-task:1) run_task_export "$command" "$1" record-export-root ;;
  publish-report:1) run_task_export "$command" "$1" report-collection-root ;;
  import-reports:0|index-reports:0)
    record_export_configured report-collection-root || die "configure report-collection-root first"
    run_engine "$command"
    ;;
  *) usage ;;
esac
