#!/usr/bin/env bash
# Claude PreToolUse gate that blocks the Bash tool after a context compaction
# until the agent says, in an assistant text block written after that
# compaction, that it has read the full digest.
#
# The Claude transcript named by the hook payload is the only record of what
# the agent said, so the gate reads it and keeps no state of its own.
# The last `compact_boundary` entry marks the latest compaction.
# Only assistant text blocks after it count; thinking, tool input, tool
# results, and the compaction summary do not.
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
#
# An interactive Claude session writes the current assistant message to the
# transcript only after the hook returns. A phrase written in the same message
# as the gated Bash call is therefore missing when the gate checks, so the
# requirement tells the agent to say it beside its last Read of the digest or to
# retry. When the phrase is missing, the gate still waits up to 1 second for the
# call's own tool_use_id to reach the transcript, for a writer that lands the
# message while the hook runs.
set -u

PHRASE='I have read the full digest'
REQUIREMENT="The full digest must be read. Every single session that skips the digest makes mistakes which causes frustration for the Captain. When you are done say exactly 'I have read the full digest'. Say it in the same message as your last Read of the digest, or retry the Bash call after saying it. Punctuation makes no difference."
BOUNDARY='"subtype":"compact_boundary"'
FLUSH_WAIT_TENTHS=10

PAYLOAD=$(cat 2>/dev/null || true)
[ -n "$PAYLOAD" ] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P) || exit 0
FM_ROOT=${FM_ROOT_OVERRIDE:-$(CDPATH='' cd -- "$SCRIPT_DIR/.." 2>/dev/null && pwd -P)} || exit 0
FM_HOME=${FM_HOME:-${FM_ROOT_OVERRIDE:-$FM_ROOT}}
STATE=${FM_STATE_OVERRIDE:-$FM_HOME/state}

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
TOOL_USE_ID=$(printf '%s' "$PAYLOAD" | jq -r '.tool_use_id // empty' 2>/dev/null) || TOOL_USE_ID=

# Prints the line number of the last compaction boundary.
# Fails when the session was never compacted.
last_compaction_line() {
  local line
  line=$(grep -n -F -- "$BOUNDARY" "$TRANSCRIPT" 2>/dev/null | tail -n 1 | cut -d: -f1)
  [ -n "$line" ] || return 1
  printf '%s\n' "$line"
}

# grep keeps only the boundary and lines that contain the phrase, so jq never
# parses the whole session. Each line is parsed on its own so a partly written
# final line is skipped. An entry stamped earlier than the boundary was carried
# over from before the compaction and does not count.
phrase_said_since() {  # <boundary-line>
  tail -n "+$1" -- "$TRANSCRIPT" | grep -F -e "$BOUNDARY" -e "$PHRASE" \
    | jq -R -n -e --arg phrase "$PHRASE" '
      [inputs | fromjson? // empty] as $entries
      | ($entries | map(select(.type == "system" and .subtype == "compact_boundary")) | last | .timestamp // "") as $since
      | any($entries[];
          .type == "assistant"
          and (.isSidechain // false) == false
          and (.timestamp // "") >= $since
          and ((.message.content // []) | if type == "array" then any(.[]; .type == "text" and ((.text // "") | contains($phrase))) else false end))
    ' >/dev/null 2>&1
}

BOUNDARY_LINE=$(last_compaction_line) || exit 0
phrase_said_since "$BOUNDARY_LINE" && exit 0

if [ -n "$TOOL_USE_ID" ]; then
  waited=0
  while [ "$waited" -lt "$FLUSH_WAIT_TENTHS" ] \
    && ! tail -n "+$BOUNDARY_LINE" -- "$TRANSCRIPT" | grep -q -F -- "\"$TOOL_USE_ID\""; do
    sleep 0.1
    waited=$((waited + 1))
    BOUNDARY_LINE=$(last_compaction_line) || exit 0
  done
  phrase_said_since "$BOUNDARY_LINE" && exit 0
fi

printf '%s\n' "$REQUIREMENT" >&2
exit 2
