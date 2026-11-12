#!/usr/bin/env bash
# Claude PreToolUse gate that blocks the Bash tool after a context compaction
# until a Bash call's description says "we've compacted".
#
# The agent first tells the captain in chat:
#   Captain, we've compacted - <one line on the current situation>
# and puts the same words in its next Bash call's description. The chat line is
# for the captain and the gate does not read it. The gate reads only the
# description, which arrives in this hook's own payload as
# tool_input.description, so the pass never waits on a transcript write.
# Case, whitespace, and punctuation in the description make no difference.
#
# The last `compact_boundary` entry in the Claude transcript named by the
# payload marks the latest compaction. When a call passes, the gate records that
# boundary's uuid in $STATE/.digest-gate-ack, and later Bash calls pass while
# the latest boundary is still the recorded one. A new compaction writes a new
# boundary, so the description is required again.
# The record is the gate's own because Claude writes the passed call to the
# transcript late, often after the next Bash call is already gated.
#
# Usage: <Claude PreToolUse JSON on stdin> | bin/fm-digest-read-gate.sh
#
# Exit/output contract:
#   ALLOW - exit 0 and no output.
#   DENY - exit 2 and the requirement on stderr, which Claude shows the model.
#   INERT - exit 0 with no output when the home is not a genuine primary home,
#           the session is a no-mistakes gate agent, the payload is Cursor's
#           duplicate of this Claude registration, or the transcript has no
#           compaction. These are the sessions that receive no digest.
#   FAIL OPEN - exit 0 for an empty payload, missing jq, or a missing or
#               unreadable transcript.
# A failed write of the ack record still allows the passing call. The next call
# then needs the description again.
set -u

REQUIREMENT="Context was compacted. Read the whole session-start output first. Then tell the captain in chat: \"Captain, we've compacted - <one line on the current situation>\". Put the same words in this Bash call's description and run it again."
BOUNDARY='"subtype":"compact_boundary"'

PAYLOAD=$(cat 2>/dev/null || true)
[ -n "$PAYLOAD" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P) || exit 0
FM_ROOT=${FM_ROOT_OVERRIDE:-$(CDPATH='' cd -- "$SCRIPT_DIR/.." 2>/dev/null && pwd -P)} || exit 0
FM_HOME=${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}
STATE=${FM_STATE_OVERRIDE:-$FM_HOME/state}
ACK="$STATE/.digest-gate-ack"

# shellcheck source=bin/fm-hook-host-lib.sh
. "$SCRIPT_DIR/fm-hook-host-lib.sh"
# shellcheck source=bin/fm-gate-refuse-lib.sh
. "$SCRIPT_DIR/fm-gate-refuse-lib.sh"
# shellcheck source=bin/fm-primary-scope-lib.sh
. "$SCRIPT_DIR/fm-primary-scope-lib.sh"

# The same eligibility checks bin/fm-sessionstart-run.sh applies before it
# emits the digest.
fm_hook_payload_is_foreign_host "$PAYLOAD" && exit 0
fm_is_gate_agent "$FM_ROOT" && exit 0
fm_primary_scope_matches "$FM_ROOT" "$STATE" || exit 0

TRANSCRIPT=$(printf '%s' "$PAYLOAD" | jq -r '.transcript_path // empty' 2>/dev/null) || exit 0
[ -n "$TRANSCRIPT" ] && [ -f "$TRANSCRIPT" ] && [ -r "$TRANSCRIPT" ] || exit 0

# The last boundary line is parsed on its own, so jq never reads the whole
# session. A line still being written yields an empty key.
BOUNDARY_ENTRY=$(grep -F -- "$BOUNDARY" "$TRANSCRIPT" 2>/dev/null | tail -n 1)
[ -n "$BOUNDARY_ENTRY" ] || exit 0
BOUNDARY_KEY=$(printf '%s' "$BOUNDARY_ENTRY" | jq -r '.uuid // empty' 2>/dev/null) || BOUNDARY_KEY=

if [ -n "$BOUNDARY_KEY" ] && [ "$(cat "$ACK" 2>/dev/null)" = "$BOUNDARY_KEY" ]; then
  exit 0
fi

if printf '%s' "$PAYLOAD" | jq -e '
    (.tool_input.description // "") | tostring | ascii_downcase
    | gsub("[^a-z0-9\\s]"; "") | gsub("\\s+"; " ")
    | contains("weve compacted")
  ' >/dev/null 2>&1; then
  if [ -n "$BOUNDARY_KEY" ]; then
    tmp=$(mktemp "$ACK.XXXXXX" 2>/dev/null) \
      && printf '%s\n' "$BOUNDARY_KEY" > "$tmp" \
      && { mv -f -- "$tmp" "$ACK" 2>/dev/null || rm -f -- "$tmp"; }
  fi
  exit 0
fi

printf '%s\n' "$REQUIREMENT" >&2
exit 2
