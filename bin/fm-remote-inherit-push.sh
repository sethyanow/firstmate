#!/usr/bin/env bash
# Push the declared inherited-material allowlist to one remote secondmate route.
# Usage: fm-remote-inherit-push.sh <secondmate-id> <generation>
#
# The item set is derived from the ONE declared owner
# (FM_INHERITABLE_CONFIG in bin/fm-config-inherit-lib.sh), the same declaration
# the receiving bin/fm-remote-inherit.sh enforces, so the two implementations in
# one code revision cannot drift silently. Different local and remote revisions
# fail closed as documented by that owner. FM_CONFIG_INHERIT_LIVE=1 marks a live
# convergence push into an already-running home and skips session-scoped items,
# exactly as the local propagation path does. Every item is validated and
# snapshotted locally before any is sent.
#
# The items then travel in one remote job, `fm-remote-inherit.sh batch`, whose
# header owns the stream format. The remote job worker runs one home's jobs one
# after another, so a job per item would pay the whole queue round trip once per
# item. Items are split across more than one batch only when they would exceed
# the job input bound (FM_REMOTE_JOB_MAX_BYTES in bin/fm-remote-job-lib.sh). An
# item too large for any batch travels alone as `put`. A receiver whose code
# root predates batches refuses the batch form with its usage status, 2. The
# push then sends that remote's items one at a time with put and absent, so a
# primary can update before its remote code roots do. Sending stops at the
# first failed batch or item.
#
# The caller holds the sender transaction lock. Before sending, read the
# receiver's committed high-water mark and durably advance a lost or restored
# sender counter beyond it. For older receivers, a query usage refusal (exit 2)
# uses floor zero and leaves write-time generation checks intact; counter
# recovery requires a receiver that supports the query. Other query failures
# and malformed replies fail closed.
set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FM_ROOT="${FM_ROOT_OVERRIDE:-$(cd "$SCRIPT_DIR/.." && pwd)}"
FM_HOME="${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}"
CONFIG="${FM_CONFIG_OVERRIDE:-$FM_HOME/config}"
DATA="${FM_DATA_OVERRIDE:-$FM_HOME/data}"
STATE="${FM_STATE_OVERRIDE:-$FM_HOME/state}"

# shellcheck source=bin/fm-secondmate-registry-lib.sh
. "$SCRIPT_DIR/fm-secondmate-registry-lib.sh"
# shellcheck source=bin/fm-config-inherit-lib.sh
. "$SCRIPT_DIR/fm-config-inherit-lib.sh"
# shellcheck source=bin/fm-remote-job-lib.sh
. "$SCRIPT_DIR/fm-remote-job-lib.sh"
# shellcheck source=bin/fm-secondmate-nudge-lib.sh
. "$SCRIPT_DIR/fm-secondmate-nudge-lib.sh"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
sha256_file() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi
}
file_link_count() {
  if [ "$(uname)" = Darwin ]; then /usr/bin/stat -f %l "$1" 2>/dev/null; else stat -c %h "$1" 2>/dev/null; fi
}
[ "$#" -eq 2 ] || { echo "usage: fm-remote-inherit-push.sh <secondmate-id> <generation>" >&2; exit 2; }
ID=$1
GENERATION=$2
case "$ID" in ''|*[!A-Za-z0-9._-]*) die "invalid secondmate id: $ID" ;; esac
case "$GENERATION" in ''|*[!0-9]*) die "generation must be a positive integer" ;; esac
[ "${#GENERATION}" -le 18 ] && [ "$GENERATION" -ge 1 ] || die "generation is outside the supported range"
REMOTE=$(secondmate_registry_field "$DATA/secondmates.md" "$ID" remote 2>/dev/null || true)
[ "$REMOTE" = 1 ] || die "secondmate $ID is not a remote route"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/fm-remote-inherit-push.XXXXXX") || die "cannot create inheritance staging directory"
trap 'rm -rf -- "$TMP"' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
EMPTY="$TMP/empty"
: > "$EMPTY"
EMPTY_HASH=$(sha256_file "$EMPTY") || die "cannot hash empty inheritance payload"

ITEMS=$(fm_config_inherit_items)
# Phase 1 is local: every item is classified, validated, and snapshotted in
# allowlist order before anything is sent, so a refusal here leaves the remote
# home untouched. PLAN holds one record per item: skip, put, or absent.
N=0
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  N=$((N + 1))
  printf '%s\n' "$rel" > "$TMP/item.$N.rel"
  if [ "${FM_CONFIG_INHERIT_LIVE:-0}" = 1 ]; then
    case "$rel" in
      config/*)
        if fm_config_inherit_item_session_scoped "${rel#config/}"; then
          printf 'skip\n' > "$TMP/item.$N.plan"
          continue
        fi
        ;;
    esac
  fi
  case "$rel" in
    config/firstmate-role)
      source="$TMP/firstmate-role.resolved"
      fm_role_inherit_payload "$FM_HOME" "$CONFIG" "$source" || die "cannot resolve the primary role"
      ;;
    config/*) source="$CONFIG/${rel#config/}" ;;
    data/*) source="$DATA/${rel#data/}" ;;
  esac
  source_present=$(fm_config_source_present "$source") || exit 1
  if [ "$source_present" = 1 ]; then
    [ -f "$source" ] && [ ! -L "$source" ] || die "inherited source is unsafe: $source"
    [ "$(file_link_count "$source")" = 1 ] || die "inherited source is hardlinked: $source"
    if [ "$rel" = data/captain-shared.md ]; then
      if ! missing=$(shared_captain_header_valid "$source"); then
        reason="shared captain preferences have no valid primary-authoritative header"
        [ -z "$missing" ] || reason="$reason: missing \"$missing\""
        die "$reason"
      fi
    fi
    snapshot="$TMP/item.$N.snapshot"
    cp -p -- "$source" "$snapshot" || die "cannot snapshot inherited source: $source"
    [ -f "$snapshot" ] && [ ! -L "$snapshot" ] || die "inherited source snapshot is unsafe: $source"
    bytes=$(LC_ALL=C wc -c < "$snapshot" | tr -d ' ')
    hash=$(sha256_file "$snapshot") || die "cannot hash inherited source: $source"
    printf 'put %s %s\n' "$bytes" "$hash" > "$TMP/item.$N.plan"
  else
    printf 'absent\n' > "$TMP/item.$N.plan"
  fi
done <<EOF
$ITEMS
EOF

# Query only after local validation, before any payload can change the receiver.
query_rc=0
REMOTE_GENERATION=$("$SCRIPT_DIR/fm-on.sh" "$ID" fm-remote-inherit.sh generation) || query_rc=$?
case "$query_rc" in
  0) ;;
  2) REMOTE_GENERATION=0 ;;
  *) exit "$query_rc" ;;
esac
case "$REMOTE_GENERATION" in ''|*[!0-9]*) die "receiver inheritance generation is malformed" ;; esac
[ "${#REMOTE_GENERATION}" -le 18 ] || die "receiver inheritance generation is outside the supported range"
if [ "$REMOTE_GENERATION" -ge "$GENERATION" ]; then
  GENERATION=$(fm_remote_inherit_generation_next "$STATE" "$ID" "$REMOTE_GENERATION") \
    || die "cannot recover sender inheritance generation"
fi

# Phase 2 sends the items in allowlist order. A session-scoped item is never
# sent and reads as unchanged.
USAGE_EXIT=2
STATUS=0

send_item() {  # <n>: one item through the per-item put or absent form
  local n=$1 rel plan bytes hash rc=0
  rel=$(cat "$TMP/item.$n.rel")
  read -r plan bytes hash < "$TMP/item.$n.plan"
  if [ "$plan" = put ]; then
    "$SCRIPT_DIR/fm-on.sh" --stdin "$ID" fm-remote-inherit.sh put "$rel" "$bytes" "$hash" "$GENERATION" \
      < "$TMP/item.$n.snapshot" > "$TMP/send.out" 2> "$TMP/send.err" || rc=$?
  else
    "$SCRIPT_DIR/fm-on.sh" "$ID" fm-remote-inherit.sh absent "$rel" 0 "$EMPTY_HASH" "$GENERATION" \
      < /dev/null > "$TMP/send.out" 2> "$TMP/send.err" || rc=$?
    if [ "$rc" = 1 ] && grep -Fxq -- "error: path is not inherited material: $rel" "$TMP/send.err"; then
      printf 'unchanged: %s\n' "$rel" > "$TMP/send.out"
      : > "$TMP/send.err"
      rc=0
    fi
  fi
  cat "$TMP/send.out"
  cat "$TMP/send.err" >&2
  return "$rc"
}

# Send the items listed in $TMP/batch.items as one batch. Returns the remote
# status; USAGE_EXIT means this receiver predates batches and nothing ran.
send_batch() {
  local n rel plan bytes hash rc=0
  {
    printf '%s\n' "$BATCH_HEADER"
    while IFS= read -r n; do
      rel=$(cat "$TMP/item.$n.rel")
      read -r plan bytes hash < "$TMP/item.$n.plan"
      if [ "$plan" = put ]; then
        printf 'put %s %s %s\n' "$rel" "$bytes" "$hash"
      else
        printf 'absent %s 0 %s\n' "$rel" "$EMPTY_HASH"
      fi
    done < "$TMP/batch.items"
    printf '%s\n' "$BATCH_END"
    while IFS= read -r n; do
      [ ! -f "$TMP/item.$n.snapshot" ] || cat "$TMP/item.$n.snapshot"
    done < "$TMP/batch.items"
  } > "$TMP/batch.stream"
  "$SCRIPT_DIR/fm-on.sh" --stdin "$ID" fm-remote-inherit.sh batch "$GENERATION" \
    < "$TMP/batch.stream" > "$TMP/send.out" 2> "$TMP/send.err" || rc=$?
  [ "$rc" -eq "$USAGE_EXIT" ] && return "$rc"
  cat "$TMP/send.out"
  cat "$TMP/send.err" >&2
  return "$rc"
}

# The bytes one item adds to a batch stream: its record line and its payload.
record_bytes() {  # <n>
  local rel plan bytes hash line
  rel=$(cat "$TMP/item.$1.rel")
  read -r plan bytes hash < "$TMP/item.$1.plan"
  if [ "$plan" = put ]; then
    line="put $rel $bytes $hash"
    printf '%s\n' "$(( ${#line} + 1 + bytes ))"
  else
    line="absent $rel 0 $EMPTY_HASH"
    printf '%s\n' "$(( ${#line} + 1 ))"
  fi
}

flush_batch() {
  local n rc=0
  [ -s "$TMP/batch.items" ] || return 0
  send_batch || rc=$?
  if [ "$rc" -eq "$USAGE_EXIT" ]; then
    RECEIVER_TAKES_BATCHES=0
    rc=0
    while IFS= read -r n; do
      send_item "$n" || { rc=$?; break; }
    done < "$TMP/batch.items"
  fi
  : > "$TMP/batch.items"
  BATCH_BYTES=$BATCH_FRAME_BYTES
  return "$rc"
}

queue_item() {  # <n>
  local size=0
  if [ "$RECEIVER_TAKES_BATCHES" -eq 1 ]; then
    size=$(record_bytes "$1")
    # An item that cannot share the current batch, or travels alone, sends
    # what is queued first so items still apply in allowlist order.
    [ "$((BATCH_BYTES + size))" -le "$FM_REMOTE_JOB_MAX_BYTES" ] || flush_batch || return $?
  fi
  if [ "$RECEIVER_TAKES_BATCHES" -eq 0 ] || [ "$((BATCH_FRAME_BYTES + size))" -gt "$FM_REMOTE_JOB_MAX_BYTES" ]; then
    send_item "$1"
    return $?
  fi
  printf '%s\n' "$1" >> "$TMP/batch.items"
  BATCH_BYTES=$((BATCH_BYTES + size))
}

# RECEIVER_TAKES_BATCHES drops to 0 once the receiver refuses the batch form.
# The frame is the header line and the end line that fm-remote-inherit.sh reads.
RECEIVER_TAKES_BATCHES=1
BATCH_HEADER='fm-remote-inherit-batch 1'
BATCH_END=end
BATCH_FRAME_BYTES=$(( ${#BATCH_HEADER} + ${#BATCH_END} + 2 ))
BATCH_BYTES=$BATCH_FRAME_BYTES
: > "$TMP/batch.items"
LC_ALL=C
n=1
while [ "$n" -le "$N" ]; do
  if [ "$(cut -d' ' -f1 "$TMP/item.$n.plan")" = skip ]; then
    printf 'unchanged: %s\n' "$(cat "$TMP/item.$n.rel")"
  else
    queue_item "$n" || { STATUS=$?; break; }
  fi
  n=$((n + 1))
done
[ "$STATUS" -ne 0 ] || flush_batch || STATUS=$?
exit "$STATUS"
