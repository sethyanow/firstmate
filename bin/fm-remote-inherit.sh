#!/usr/bin/env bash
# Apply primary-authoritative inherited items inside the selected remote home.
#
# Usage:
#   fm-remote-inherit.sh put <allowlisted-relative-path> <bytes> <sha256> <generation> < stdin
#   fm-remote-inherit.sh absent <allowlisted-relative-path> 0 <empty-sha256> <generation>
#   fm-remote-inherit.sh batch <generation> < stream
#
# Only the inherited-material allowlist is writable or removable. Writes are
# atomic ordinary-file replacements. data/captain-shared.md is read-only and is
# quarantined before removal or before replacing bytes not last published here.
#
# batch applies many items in one remote job, in stream order, and stops at the
# first item that fails; items applied before it stay applied, exactly as
# separate put and absent calls would leave them. The stream is a manifest
# followed by the put payloads concatenated in manifest order:
#   fm-remote-inherit-batch 1
#   put <path> <bytes> <sha256>
#   absent <path> 0 <empty-sha256>
#   end
#   <payload bytes>
# The whole stream is checked before any item is applied: a missing end line,
# a malformed record, or payload bytes that do not add up to the manifest refuse
# the batch. An absent record for a path this code root does not declare reads
# as unchanged, because this home cannot read that item anyway.
set -eu

FM_HOME=${FM_HOME:?FM_HOME is required}
MAX_BYTES=1048576
BATCH_MAX_RECORDS=256
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=bin/fm-wake-lib.sh
. "$SCRIPT_DIR/fm-wake-lib.sh"
# shellcheck source=bin/fm-config-inherit-lib.sh
. "$SCRIPT_DIR/fm-config-inherit-lib.sh"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }
usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
file_link_count() {
  if [ "$(uname)" = Darwin ]; then /usr/bin/stat -f %l "$1" 2>/dev/null; else stat -c %h "$1" 2>/dev/null; fi
}
sha256_file() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi
}
# Writable set, derived from the ONE declared inherited-material owner
# (FM_INHERITABLE_CONFIG in bin/fm-config-inherit-lib.sh), so this code root's
# receiver and sender cannot drift silently. This runs under the remote
# entrypoint's fixed empty environment, so the declaration is this code root's
# own, never something the caller can widen over SSH; a caller from a different
# revision must match it or the transfer fails closed.
ALLOWED_ITEMS=$(fm_config_inherit_items)
allowed() {
  local candidate
  while IFS= read -r candidate; do
    [ "$candidate" = "$1" ] && return 0
  done <<EOF
$ALLOWED_ITEMS
EOF
  return 1
}

validate_generation() {
  case "$GENERATION" in ''|*[!0-9]*) die "generation must be a positive integer" ;; esac
  [ "${#GENERATION}" -le 18 ] && [ "$GENERATION" -ge 1 ] || die "generation is outside the supported range"
}

HOME_REAL=
LOCK=
TMP=
GENERATION_TMP=
BATCH_DIR=
cleanup() {
  [ -z "$TMP" ] || rm -f -- "$TMP"
  [ -z "$GENERATION_TMP" ] || rm -f -- "$GENERATION_TMP"
  [ -z "$LOCK" ] || fm_lock_release "$LOCK" || true
  [ -z "$BATCH_DIR" ] || rm -rf -- "$BATCH_DIR"
}
trap cleanup EXIT

commit_generation() {
  local existing_generation existing_bytes existing_hash existing_command
  if [ -e "$GENERATION_FILE" ] || [ -L "$GENERATION_FILE" ]; then
    [ -f "$GENERATION_FILE" ] && [ ! -L "$GENERATION_FILE" ] || die "inheritance generation record is unsafe"
    {
      IFS= read -r existing_generation \
        && IFS= read -r existing_bytes \
        && IFS= read -r existing_hash \
        && IFS= read -r existing_command \
        && ! IFS= read -r
    } < "$GENERATION_FILE" || die "inheritance generation record is malformed"
    case "$existing_generation" in ''|*[!0-9]*) die "inheritance generation record is malformed" ;; esac
    [ "${#existing_generation}" -le 18 ] || die "inheritance generation record is malformed"
    case "$existing_bytes" in ''|*[!0-9]*) die "inheritance generation record is malformed" ;; esac
    case "$existing_hash" in ''|*[!A-Fa-f0-9]*) die "inheritance generation record is malformed" ;; esac
    [ "${#existing_hash}" -eq 64 ] || die "inheritance generation record is malformed"
    case "$existing_command" in put|absent) ;; *) die "inheritance generation record is malformed" ;; esac
    [ "$existing_command" != put ] || LAST_PUBLISHED_HASH=$(printf '%s' "$existing_hash" | tr 'A-F' 'a-f')
    if [ "$existing_generation" -gt "$GENERATION" ]; then
      die "inheritance write generation is superseded"
    fi
    if [ "$existing_generation" -eq "$GENERATION" ]; then
      [ "$existing_bytes" = "$EXPECTED_BYTES" ] \
        && [ "$existing_hash" = "$EXPECTED_HASH" ] \
        && [ "$existing_command" = "$COMMAND" ] \
        || die "inheritance generation conflicts with its committed payload"
      return 0
    fi
  fi
  GENERATION_TMP=$(umask 077; mktemp "$PARENT_REAL/.inherit-generation.XXXXXX") \
    || die "cannot stage inheritance generation"
  printf '%s\n%s\n%s\n%s\n' "$GENERATION" "$EXPECTED_BYTES" "$EXPECTED_HASH" "$COMMAND" > "$GENERATION_TMP" \
    || die "cannot write inheritance generation"
  chmod 600 "$GENERATION_TMP" || die "cannot secure inheritance generation"
  mv -f -- "$GENERATION_TMP" "$GENERATION_FILE" || die "cannot publish inheritance generation"
  GENERATION_TMP=
}

# True when the destination still holds the bytes this receiver last published,
# so replacing it is ordinary convergence rather than destination drift.
dest_matches_last_published() {
  local actual
  [ -n "$LAST_PUBLISHED_HASH" ] && [ -f "$DEST" ] || return 1
  actual=$(sha256_file "$DEST") || return 1
  [ "$actual" = "$LAST_PUBLISHED_HASH" ]
}

quarantine_shared() {
  local reason=$1 quarantine stamp base n=0
  [ "$REL" = data/captain-shared.md ] && [ -f "$DEST" ] || return 0
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  base="$HOME_REAL/data/captain-shared.md.remote-quarantine-$stamp-$$"
  quarantine=$base
  while [ -e "$quarantine" ] || [ -L "$quarantine" ]; do
    n=$((n + 1))
    quarantine="$base.$n"
  done
  cp -p -- "$DEST" "$quarantine" || die "cannot quarantine divergent shared captain preferences"
  chmod 600 "$quarantine" || die "cannot secure shared-preference quarantine"
  printf 'quarantined: %s (%s)\n' "${quarantine#"$HOME_REAL/"}" "$reason" >&2
}

finish_item() {
  fm_lock_release "$LOCK" || true
  LOCK=
}

# Apply one item at GENERATION. A put payload is read from stdin. Any failure
# exits the whole command, which stops a batch at its first failed item.
apply_item() {  # <put|absent> <relative-path> <bytes> <sha256>
  COMMAND=$1
  REL=$2
  EXPECTED_BYTES=$3
  EXPECTED_HASH=$4
  LAST_PUBLISHED_HASH=
  allowed "$REL" || die "path is not inherited material: $REL"
  case "$EXPECTED_BYTES" in ''|*[!0-9]*) die "expected bytes must be a nonnegative integer" ;; esac
  [ "${#EXPECTED_BYTES}" -le 10 ] || die "expected bytes exceed the byte bound"
  [ "$EXPECTED_BYTES" -le "$MAX_BYTES" ] || die "expected bytes exceed the byte bound"
  case "$EXPECTED_HASH" in ''|*[!A-Fa-f0-9]*) die "expected SHA-256 is invalid" ;; esac
  [ "${#EXPECTED_HASH}" -eq 64 ] || die "expected SHA-256 has the wrong length"
  EXPECTED_HASH=$(printf '%s' "$EXPECTED_HASH" | tr 'A-F' 'a-f')
  PARENT="$HOME_REAL/$(dirname "$REL")"
  # The captain accepts this config/data parent TOCTOU within Firstmate's single-user trust boundary.
  [ ! -L "$PARENT" ] || die "inherited destination parent is a symlink"
  mkdir -p "$PARENT" || die "cannot create inherited destination parent"
  PARENT_REAL=$(CDPATH='' cd -- "$PARENT" && pwd -P)
  case "$PARENT_REAL" in "$HOME_REAL/config"|"$HOME_REAL/data") ;; *) die "inherited destination escapes FM_HOME" ;; esac
  DEST="$PARENT_REAL/$(basename "$REL")"
  [ ! -L "$DEST" ] || die "inherited destination is a symlink"
  if [ -e "$DEST" ]; then
    [ -f "$DEST" ] || die "inherited destination is not a regular file"
    [ "$(file_link_count "$DEST")" = 1 ] || die "inherited destination is hardlinked"
  fi

  BASE=$(basename "$REL")
  GENERATION_FILE="$PARENT_REAL/.fm-inherit-$BASE.generation"
  fm_lock_acquire_wait "$PARENT_REAL/.fm-inherit-$BASE.lock" || die "cannot lock inherited destination"
  LOCK="$PARENT_REAL/.fm-inherit-$BASE.lock"

  case "$COMMAND" in
    put)
      TMP=$(umask 077; mktemp "$PARENT_REAL/.inherit.XXXXXX") || die "cannot stage inherited material"
      head -c "$((MAX_BYTES + 1))" > "$TMP" || die "cannot read inherited material"
      BYTES=$(LC_ALL=C wc -c < "$TMP" | tr -d ' ')
      [ "$BYTES" -le "$MAX_BYTES" ] || die "inherited material exceeds the byte bound"
      [ "$BYTES" -eq "$EXPECTED_BYTES" ] || die "inherited material length does not match its commitment"
      ACTUAL_HASH=$(sha256_file "$TMP") || die "cannot hash inherited material"
      [ "$ACTUAL_HASH" = "$EXPECTED_HASH" ] || die "inherited material digest does not match its commitment"
      commit_generation
      if [ -f "$DEST" ] && cmp -s "$TMP" "$DEST"; then
        [ "$REL" != data/captain-shared.md ] || chmod 444 "$DEST"
        rm -f -- "$TMP"
        TMP=
        printf 'unchanged: %s\n' "$REL"
        finish_item
        return 0
      fi
      dest_matches_last_published || quarantine_shared replaced
      chmod 600 "$TMP" || die "cannot secure inherited material"
      mv -f -- "$TMP" "$DEST" || die "cannot publish inherited material"
      TMP=
      [ "$REL" != data/captain-shared.md ] || chmod 444 "$DEST"
      printf 'pushed: %s\n' "$REL"
      ;;
    absent)
      [ "$EXPECTED_BYTES" -eq 0 ] || die "absent inheritance has a nonzero payload commitment"
      [ "$EXPECTED_HASH" = "$EMPTY_HASH" ] || die "absent inheritance digest is not the empty payload"
      commit_generation
      if [ ! -e "$DEST" ]; then
        printf 'unchanged: %s\n' "$REL"
        finish_item
        return 0
      fi
      quarantine_shared removed
      rm -f -- "$DEST" || die "cannot remove absent inherited material"
      printf 'removed: %s\n' "$REL"
      ;;
    *) usage ;;
  esac
  finish_item
}

# Check the whole batch stream, then apply its records in order.
apply_batch() {
  local stream header_bytes payload_bytes total_bytes offset line n=0 i
  local command rel bytes hash extra payload
  local LC_ALL=C
  BATCH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/fm-remote-inherit-batch.XXXXXX") || die "cannot stage the inheritance batch"
  stream="$BATCH_DIR/stream"
  head -c "$((MAX_BYTES + 1))" > "$stream" || die "cannot read the inheritance batch"
  total_bytes=$(wc -c < "$stream" | tr -d ' ')
  [ "$total_bytes" -le "$MAX_BYTES" ] || die "inheritance batch exceeds the byte bound"
  header_bytes=0
  payload_bytes=0
  {
    IFS= read -r line || die "inheritance batch is empty"
    [ "$line" = 'fm-remote-inherit-batch 1' ] || die "inheritance batch has an unsupported header"
    header_bytes=$(( ${#line} + 1 ))
    while :; do
      IFS= read -r line || die "inheritance batch manifest has no end line"
      header_bytes=$(( header_bytes + ${#line} + 1 ))
      [ "$line" != end ] || break
      n=$((n + 1))
      [ "$n" -le "$BATCH_MAX_RECORDS" ] || die "inheritance batch has too many records"
      read -r command rel bytes hash extra <<EOF || true
$line
EOF
      [ -n "$hash" ] && [ -z "$extra" ] || die "inheritance batch record is malformed: $line"
      case "$command" in put|absent) ;; *) die "inheritance batch record is malformed: $line" ;; esac
      case "$bytes" in ''|*[!0-9]*) die "inheritance batch record is malformed: $line" ;; esac
      [ "${#bytes}" -le 10 ] && [ "$bytes" -le "$MAX_BYTES" ] || die "expected bytes exceed the byte bound"
      [ "$command" = put ] || [ "$bytes" -eq 0 ] || die "absent inheritance has a nonzero payload commitment"
      printf '%s %s %s %s\n' "$command" "$rel" "$bytes" "$hash" > "$BATCH_DIR/record.$n"
      [ "$command" != put ] || payload_bytes=$((payload_bytes + bytes))
    done
  } < "$stream"
  [ "$((header_bytes + payload_bytes))" -eq "$total_bytes" ] \
    || die "inheritance batch payload length does not match its manifest"

  offset=$((header_bytes + 1))
  i=1
  while [ "$i" -le "$n" ]; do
    read -r command rel bytes hash < "$BATCH_DIR/record.$i"
    if [ "$command" = put ]; then
      payload="$BATCH_DIR/payload"
      tail -c "+$offset" "$stream" | head -c "$bytes" > "$payload" || die "cannot read the inheritance batch payload"
      offset=$((offset + bytes))
      apply_item put "$rel" "$bytes" "$hash" < "$payload"
    elif allowed "$rel"; then
      apply_item absent "$rel" "$bytes" "$hash" < /dev/null
    else
      printf 'unchanged: %s\n' "$rel"
    fi
    i=$((i + 1))
  done
}

[ "$#" -ge 1 ] || usage
case "$1" in
  put|absent) [ "$#" -eq 5 ] || usage; GENERATION=$5 ;;
  batch) [ "$#" -eq 2 ] || usage; GENERATION=$2 ;;
  *) usage ;;
esac
validate_generation
HOME_REAL=$(CDPATH='' cd -- "$FM_HOME" 2>/dev/null && pwd -P) || die "FM_HOME is unavailable"
EMPTY_HASH=$(sha256_file /dev/null) || die "cannot hash empty inheritance payload"
case "$1" in
  batch) apply_batch ;;
  *) apply_item "$1" "$2" "$3" "$4" ;;
esac
