#!/usr/bin/env bash
# Behavior tests for the Claude PreToolUse gate that blocks the Bash tool after
# a compaction until a Bash call's description says "we've compacted".
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

GATE="$ROOT/bin/fm-digest-read-gate.sh"
TMP_ROOT=$(fm_test_tmproot fm-digest-read-gate-tests)
PRIMARY="$TMP_ROOT/primary"
STATE="$PRIMARY/state"
OUT="$TMP_ROOT/out"
ERR="$TMP_ROOT/err"
LINE="Captain, we've compacted - two ships in flight, one PR waiting on review"
PLAIN='List the state directory'

mkdir -p "$PRIMARY/bin" "$STATE"
printf '# fixture\n' > "$PRIMARY/AGENTS.md"
git -C "$PRIMARY" init -q

# Transcript entry writers, shaped like Claude Code's session JSONL.
entry_boundary() {  # <uuid> <timestamp>
  jq -cn --arg id "$1" --arg ts "$2" '{parentUuid:null,type:"system",subtype:"compact_boundary",uuid:$id,content:"Conversation compacted",timestamp:$ts}'
}
entry_text() {  # <timestamp> <text>
  jq -cn --arg ts "$1" --arg t "$2" '{type:"assistant",isSidechain:false,timestamp:$ts,message:{role:"assistant",content:[{type:"text",text:$t}]}}'
}

payload() {  # <transcript> [description]
  jq -cn --arg t "$1" --arg d "${2-}" \
    '{session_id:"s",transcript_path:$t,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:({command:"ls"} + (if $d == "" then {} else {description:$d} end))}'
}

run_gate() {  # <payload> [root] -> exit status
  local root=${2:-$PRIMARY} rc=0
  : > "$OUT"
  : > "$ERR"
  printf '%s' "$1" \
    | env -u NO_MISTAKES_GATE FM_ROOT_OVERRIDE="$root" FM_HOME="$root" FM_STATE_OVERRIDE="$root/state" \
      "$GATE" > "$OUT" 2> "$ERR" || rc=$?
  return "$rc"
}

expect_allow() {  # <label> <payload> [root]
  local rc=0
  run_gate "$2" "${3:-$PRIMARY}" || rc=$?
  [ "$rc" -eq 0 ] || fail "$1: must allow, got exit $rc: $(cat "$ERR")"
  [ ! -s "$OUT" ] || fail "$1: allow wrote stdout: $(cat "$OUT")"
  [ ! -s "$ERR" ] || fail "$1: allow wrote stderr: $(cat "$ERR")"
}

assert_states_requirement() {  # <label> <refusal>
  assert_contains "$2" "Captain, we've compacted - " "$1: the refusal must give the chat line"
  assert_contains "$2" "description" "$1: the refusal must say the words go in the call's description"
}

expect_deny() {  # <label> <payload>
  local rc=0
  run_gate "$2" || rc=$?
  [ "$rc" -eq 2 ] || fail "$1: must deny with exit 2, got $rc"
  [ ! -s "$OUT" ] || fail "$1: deny wrote stdout: $(cat "$OUT")"
  assert_states_requirement "$1" "$(cat "$ERR")"
}

# Each test compacts with its own boundary uuid, so a pass in one test never
# carries into another through the shared home.
compacted_transcript() {  # <name> <boundary-uuid>
  local t="$TMP_ROOT/$1.jsonl"
  {
    entry_text 2026-10-31T10:00:00.000Z 'before compaction'
    entry_boundary "$2" 2026-10-31T10:05:00.000Z
  } > "$t"
  printf '%s\n' "$t"
}

test_session_without_compaction_is_allowed() {
  local t="$TMP_ROOT/no-compaction.jsonl"
  entry_text 2026-10-31T10:00:00.000Z 'hello' > "$t"
  expect_allow "no compaction" "$(payload "$t")"
  pass "Bash is allowed in a session that was never compacted"
}

test_compaction_without_the_line_is_denied() {
  local t
  t=$(compacted_transcript denied boundary-denied)
  expect_deny "no description" "$(payload "$t")"
  expect_deny "plain description" "$(payload "$t" "$PLAIN")"
  expect_deny "still refused on retry" "$(payload "$t" "$PLAIN")"
  pass "Bash is denied after a compaction until a description says we've compacted, and the denial prints the requirement"
}

test_description_with_the_line_allows() {
  local t description n=0
  for description in \
    "$LINE" \
    "WE'VE COMPACTED" \
    "captain we’ve compacted" \
    "we've
compacted - picking up the review"; do
    n=$((n + 1))
    t=$(compacted_transcript "allows-$n" "boundary-allows-$n")
    expect_allow "description: $description" "$(payload "$t" "$description")"
  done
  pass "a description saying we've compacted allows Bash, whatever its case, whitespace, or punctuation"
}

test_pass_carries_over_until_the_next_compaction() {
  local t
  t=$(compacted_transcript carry boundary-carry-1)
  expect_allow "passing call" "$(payload "$t" "$LINE")"
  expect_allow "next call, plain description" "$(payload "$t" "$PLAIN")"
  entry_text 2026-10-31T10:06:00.000Z 'Working.' >> "$t"
  expect_allow "later call, no description" "$(payload "$t")"

  entry_boundary boundary-carry-2 2026-10-31T11:00:00.000Z >> "$t"
  expect_deny "first call after a new compaction" "$(payload "$t" "$PLAIN")"
  expect_allow "passing call after the new compaction" "$(payload "$t" "$LINE")"
  expect_allow "next call after the new pass" "$(payload "$t" "$PLAIN")"
  pass "once a call passes, later Bash calls pass until the next compaction requires the line again"
}

test_out_of_scope_sessions_are_inert() {
  local t child="$TMP_ROOT/child" rc=0
  t=$(compacted_transcript inert boundary-inert)

  git -C "$PRIMARY" -c user.name=fixture -c user.email=fixture@example.invalid add AGENTS.md
  git -C "$PRIMARY" -c user.name=fixture -c user.email=fixture@example.invalid commit -qm fixture
  git -C "$PRIMARY" worktree add -q -b fixture-child "$child"
  mkdir -p "$child/bin" "$child/state"
  expect_allow "task worktree" "$(payload "$t")" "$child"

  expect_allow "cursor duplicate" "$(payload "$t" | jq -c '. + {cursor_version:"2026.08.11"}')"

  : > "$OUT"
  : > "$ERR"
  payload "$t" | env -u FM_GATE_REFUSE_BYPASS NO_MISTAKES_GATE=1 FM_ROOT_OVERRIDE="$PRIMARY" FM_HOME="$PRIMARY" FM_STATE_OVERRIDE="$STATE" \
    "$GATE" > "$OUT" 2> "$ERR" || rc=$?
  [ "$rc" -eq 0 ] && [ ! -s "$ERR" ] || fail "a no-mistakes gate agent must be inert, got exit $rc: $(cat "$ERR")"
  pass "the gate is inert where no digest is emitted: task worktrees, Cursor duplicates, and gate agents"
}

test_unusable_transport_fails_open() {
  expect_allow "empty payload" ""
  expect_allow "missing transcript" "$(payload "$TMP_ROOT/absent.jsonl")"
  expect_allow "no transcript path" '{"tool_name":"Bash"}'
  pass "an empty payload or a missing transcript allows Bash"
}

# The tracked Claude registration, run as Claude runs it, reaches the gate.
test_tracked_claude_bash_registration_runs_gate() {
  local t cmd rc denied=0 p
  t=$(compacted_transcript registered boundary-registered)
  p=$(payload "$t")
  while IFS= read -r cmd; do
    rc=0
    printf '%s' "$p" \
      | env -u NO_MISTAKES_GATE -u GROK_AGENT -u GROK_HOOK_EVENT CLAUDE_PROJECT_DIR="$ROOT" \
        FM_ROOT_OVERRIDE="$PRIMARY" FM_HOME="$PRIMARY" FM_STATE_OVERRIDE="$STATE" \
        bash -c "$cmd" > "$OUT" 2> "$ERR" || rc=$?
    if [ "$rc" -eq 2 ]; then
      assert_states_requirement "tracked registration" "$(cat "$ERR")"
      denied=$((denied + 1))
    fi
  done < <(jq -r '.hooks.PreToolUse[] | select(.matcher == "Bash") | .hooks[].command' "$ROOT/.claude/settings.json")
  assert_equals 1 "$denied" "exactly one tracked Claude Bash hook must deny"
  pass "the tracked Claude Bash PreToolUse registration runs the gate"
}

fm_test_run \
  test_session_without_compaction_is_allowed \
  test_compaction_without_the_line_is_denied \
  test_description_with_the_line_allows \
  test_pass_carries_over_until_the_next_compaction \
  test_out_of_scope_sessions_are_inert \
  test_unusable_transport_fails_open \
  test_tracked_claude_bash_registration_runs_gate
