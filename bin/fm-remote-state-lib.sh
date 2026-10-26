#!/usr/bin/env bash
# fm-remote-state-lib.sh - bounded remote secondmate state reads.
#
# Source this file and call fm_remote_state <bin-dir> <secondmate-id>.
# stdout and exit status follow fm-on.sh, except 124 means the wall-clock
# limit expired and 125 means invalid configuration. Neither proves death.
# FM_SECONDMATE_PROBE_TIMEOUT sets the limit in positive integer seconds,
# default 60. Callers discard output on failure, including partial verdicts.

# shellcheck source=bin/fm-timeout-lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/fm-timeout-lib.sh"

fm_remote_state() {  # <bin-dir> <secondmate-id>
  local bin_dir=$1 id=$2 seconds=${FM_SECONDMATE_PROBE_TIMEOUT:-60}
  case "$seconds" in
    ''|0*|*[!0-9]*)
      echo 'FM_SECONDMATE_PROBE_TIMEOUT must be a positive integer' >&2
      return 125
      ;;
  esac
  fm_run_timed "$seconds" "$bin_dir/fm-on.sh" "$id" \
    fm-remote-secondmate-control.sh state "$id" < /dev/null
}
