#!/usr/bin/env bash
# Read and account for the local startup-memory budget.
# Usage:
#   fm-startup-memory-budget.sh read
#   fm-startup-memory-budget.sh report
#
# `read` prints the one validated effective budget from
# config/startup-memory-budget.  `report` prints token counts for
# data/captain.md, data/captain-shared.md, and data/learnings.md together.
# Bootstrap owns default materialization; this command never creates or repairs
# configuration, so an absent, malformed, symlinked, hardlinked, or otherwise
# unsafe value is a concrete error rather than an inferred default.
# With config/startup-memory-counter=tiktoken (one line), report uses o200k_base
# through uv run --with tiktoken, bounded to five seconds. Unavailable or invalid
# counters fall back visibly to ceil(UTF-8 bytes / 3). An absent setting keeps
# the dependency-free estimate and output unchanged. Legacy estimated_tokens
# fields hold the selected counter's counts, summed per file.
set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_ROOT="${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}"
CONFIG="${FM_CONFIG_OVERRIDE:-$FM_HOME/config}"
DATA="${FM_DATA_OVERRIDE:-$FM_HOME/data}"

# shellcheck source=bin/fm-startup-memory-budget-lib.sh
. "$SCRIPT_DIR/fm-startup-memory-budget-lib.sh"

usage() {
  sed -n '2,17{s/^# \{0,1\}//;p;}' "$0"
}

print_error() {
  printf 'startup-memory-budget: %s\n' "$1" >&2
}

read_budget() {
  if ! fm_startup_memory_budget_read "$CONFIG" >/dev/null; then
    print_error "invalid config/$FM_STARTUP_MEMORY_BUDGET_FILE - $FM_STARTUP_MEMORY_BUDGET_ERROR"
    return 1
  fi
  printf '%s\n' "$FM_STARTUP_MEMORY_BUDGET_VALUE"
}

report() {
  local budget bytes tokens presence file i total=0 shared_tokens=0 role=primary
  local estimator='ceil(UTF-8 bytes / 3) conservative-local-estimate'
  local -a file_bytes=() file_tokens=() file_presence=() paths=()
  if ! budget=$(read_budget); then
    return 2
  fi

  if [ -e "$FM_HOME/.fm-secondmate-home" ] || [ -L "$FM_HOME/.fm-secondmate-home" ]; then
    role=secondmate
  fi

  for file in captain.md captain-shared.md learnings.md; do
    if ! fm_startup_memory_measure_file "$DATA/$file" >/dev/null; then
      print_error "$FM_STARTUP_MEMORY_BUDGET_ERROR"
      return 2
    fi
    file_bytes+=("$FM_STARTUP_MEMORY_MEASURE_BYTES")
    file_tokens+=("$FM_STARTUP_MEMORY_MEASURE_TOKENS")
    file_presence+=("$FM_STARTUP_MEMORY_MEASURE_PRESENCE")
    if [ "$FM_STARTUP_MEMORY_MEASURE_PRESENCE" = present ]; then
      paths+=("$DATA/$file")
    else
      paths+=("")
    fi
  done
  if fm_startup_memory_count_tokens "$CONFIG" "${paths[@]}"; then
    for i in 0 1 2; do
      if ! fm_startup_memory_decimal_le "${FM_STARTUP_MEMORY_COUNTER_TOKENS[$i]}" "${file_bytes[$i]}"; then
        FM_STARTUP_MEMORY_COUNTER_ERROR='tiktoken returned a count exceeding input bytes'
        break
      fi
    done
    if [ -z "$FM_STARTUP_MEMORY_COUNTER_ERROR" ]; then
      file_tokens=("${FM_STARTUP_MEMORY_COUNTER_TOKENS[@]}")
      estimator='tiktoken encoding=o200k_base'
    fi
  fi
  printf 'estimator=%s\n' "$estimator"
  if [ -n "$FM_STARTUP_MEMORY_COUNTER_ERROR" ]; then
    printf 'counter_fallback=%s; using byte estimate\n' "$FM_STARTUP_MEMORY_COUNTER_ERROR"
  fi
  printf 'role=%s\n' "$role"
  printf 'effective_budget_tokens=%s\n' "$budget"
  i=0
  for file in captain.md captain-shared.md learnings.md; do
    bytes=${file_bytes[$i]}
    tokens=${file_tokens[$i]}
    presence=${file_presence[$i]}
    i=$((i + 1))
    total=$((total + tokens))
    [ "$file" != captain-shared.md ] || shared_tokens=$tokens
    printf 'file=data/%s bytes=%s estimated_tokens=%s status=%s\n' \
      "$file" "$bytes" "$tokens" "$presence"
  done
  printf 'total_estimated_tokens=%s\n' "$total"
  if fm_startup_memory_decimal_le "$total" "$budget"; then
    printf 'budget_status=within-budget\n'
  else
    printf 'budget_status=over-budget\n'
  fi
  if [ "$role" = secondmate ] \
    && ! fm_startup_memory_decimal_le "$shared_tokens" "$budget"; then
    printf 'exception=primary-owned-shared-file-alone-exceeds-budget\n'
  fi
}

case "${1:-}" in
  read)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    read_budget
    ;;
  report)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    report
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
