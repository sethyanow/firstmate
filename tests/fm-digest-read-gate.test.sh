#!/usr/bin/env bash
# Behavior tests for the Claude PreToolUse gate that blocks the Bash tool after
# a compaction until the agent says it has read the full digest.
set -u

# shellcheck source=tests/lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

GATE="$ROOT/bin/fm-digest-read-gate.sh"
TMP_ROOT=$(fm_test_tmproot fm-digest-read-gate-tests)
PRIMARY="$TMP_ROOT/primary"
STATE="$PRIMARY/state"
OUT="$TMP_ROOT/out"
ERR="$TMP_ROOT/err"
PHRASE='I have read the full digest'
REQUIREMENT="The full digest must be read. Every single session that skips the digest makes mistakes which causes frustration for the Captain. When you are done say exactly 'I have read the full digest'. Say it in the same message as your last Read of the digest, or retry the Bash call after saying it. Punctuation makes no difference."

mkdir -p "$PRIMARY/bin" "$STATE"
printf '# fixture\n' > "$PRIMARY/AGENTS.md"
git -C "$PRIMARY" init -q

now_ms() { perl -MTime::HiRes=time -e 'printf "%d\n", time * 1000'; }

# Transcript entry writers, shaped like Claude Code's session JSONL.
entry_boundary() {  # <timestamp>
  jq -cn --arg ts "$1" '{parentUuid:null,type:"system",subtype:"compact_boundary",content:"Conversation compacted",timestamp:$ts}'
}
entry_summary() {  # <timestamp> <text>
  jq -cn --arg ts "$1" --arg t "$2" '{type:"user",isCompactSummary:true,timestamp:$ts,message:{role:"user",content:$t}}'
}
entry_text() {  # <timestamp> <text>
  jq -cn --arg ts "$1" --arg t "$2" '{type:"assistant",isSidechain:false,timestamp:$ts,message:{role:"assistant",content:[{type:"text",text:$t}]}}'
}
entry_thinking() {  # <timestamp> <text>
  jq -cn --arg ts "$1" --arg t "$2" '{type:"assistant",isSidechain:false,timestamp:$ts,message:{role:"assistant",content:[{type:"thinking",thinking:$t}]}}'
}
entry_tool_use() {  # <timestamp> <tool-use-id> <command>
  jq -cn --arg ts "$1" --arg id "$2" --arg c "$3" '{type:"assistant",isSidechain:false,timestamp:$ts,message:{role:"assistant",content:[{type:"tool_use",id:$id,name:"Bash",input:{command:$c}}]}}'
}
entry_tool_result() {  # <timestamp> <tool-use-id> <text>
  jq -cn --arg ts "$1" --arg id "$2" --arg t "$3" '{type:"user",timestamp:$ts,message:{role:"user",content:[{type:"tool_result",tool_use_id:$id,content:$t}]}}'
}
entry_sidechain_text() {  # <timestamp> <text>
  jq -cn --arg ts "$1" --arg t "$2" '{type:"assistant",isSidechain:true,timestamp:$ts,message:{role:"assistant",content:[{type:"text",text:$t}]}}'
}

payload() {  # <transcript> [tool-use-id]
  jq -cn --arg t "$1" --arg id "${2:-}" \
    '{session_id:"s",transcript_path:$t,hook_event_name:"PreToolUse",tool_name:"Bash",tool_input:{command:"ls"}} + (if $id == "" then {} else {tool_use_id:$id} end)'
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

expect_deny() {  # <label> <payload>
  local rc=0
  run_gate "$2" || rc=$?
  [ "$rc" -eq 2 ] || fail "$1: must deny with exit 2, got $rc"
  [ ! -s "$OUT" ] || fail "$1: deny wrote stdout: $(cat "$OUT")"
  assert_equals "$REQUIREMENT" "$(cat "$ERR")" "$1: deny must print exactly the requirement"
}

test_session_without_compaction_is_allowed() {
  local t="$TMP_ROOT/no-compaction.jsonl"
  entry_text 2026-10-31T10:00:00.000Z 'hello' > "$t"
  expect_allow "no compaction" "$(payload "$t")"
  pass "Bash is allowed in a session that was never compacted"
}

test_compaction_without_phrase_is_denied() {
  local t="$TMP_ROOT/compacted.jsonl"
  {
    entry_text 2026-10-31T10:00:00.000Z 'before compaction'
    entry_boundary 2026-10-31T10:05:00.000Z
    entry_summary 2026-10-31T10:04:59.000Z 'This session is being continued.'
    entry_text 2026-10-31T10:05:10.000Z 'I will run a command now.'
  } > "$t"
  expect_deny "compacted, no phrase" "$(payload "$t")"
  pass "Bash is denied after a compaction until the phrase is said, and the denial prints the requirement"
}

test_phrase_after_compaction_allows() {
  local t="$TMP_ROOT/acknowledged.jsonl"
  {
    entry_boundary 2026-10-31T10:05:00.000Z
    entry_text 2026-10-31T10:05:10.000Z "$PHRASE"
    entry_text 2026-10-31T10:05:20.000Z 'Next step.'
  } > "$t"
  # A partly written final line is what Claude leaves mid-append.
  printf '{"type":"assistant","message":{"content":[{"type":"te' >> "$t"
  expect_allow "phrase said after compaction" "$(payload "$t")"
  pass "the phrase in an assistant text block after the compaction allows Bash"
}

test_phrase_before_compaction_does_not_count() {
  local t="$TMP_ROOT/stale-phrase.jsonl"
  {
    entry_boundary 2026-10-31T09:00:00.000Z
    entry_text 2026-10-31T09:00:10.000Z "$PHRASE"
    entry_boundary 2026-10-31T10:05:00.000Z
    entry_text 2026-10-31T10:05:10.000Z 'Working.'
  } > "$t"
  expect_deny "phrase only before the latest compaction" "$(payload "$t")"

  # An entry carried past the boundary keeps its pre-compaction timestamp.
  {
    entry_boundary 2026-10-31T10:05:00.000Z
    entry_text 2026-10-31T10:04:00.000Z "$PHRASE"
  } > "$t"
  expect_deny "carried-over pre-compaction entry" "$(payload "$t")"
  pass "a phrase said before the latest compaction does not allow Bash"
}

test_phrase_outside_assistant_text_does_not_count() {
  local t="$TMP_ROOT/not-text.jsonl" source
  for source in summary thinking tool_use tool_result sidechain; do
    {
      entry_boundary 2026-10-31T10:05:00.000Z
      case "$source" in
        summary) entry_summary 2026-10-31T10:05:01.000Z "$PHRASE" ;;
        thinking) entry_thinking 2026-10-31T10:05:01.000Z "$PHRASE" ;;
        tool_use) entry_tool_use 2026-10-31T10:05:01.000Z toolu_a "echo '$PHRASE'" ;;
        tool_result) entry_tool_result 2026-10-31T10:05:01.000Z toolu_a "$PHRASE" ;;
        sidechain) entry_sidechain_text 2026-10-31T10:05:01.000Z "$PHRASE" ;;
      esac
    } > "$t"
    expect_deny "phrase only in $source" "$(payload "$t")"
  done
  pass "the phrase counts only in the main agent's assistant text blocks"
}

# Claude writes the assistant text block of the current message after the
# PreToolUse hook has started. The gate must wait for that write.
test_phrase_in_same_message_waits_for_transcript_write() {
  local t="$TMP_ROOT/same-message.jsonl" writer
  entry_boundary 2026-10-31T10:05:00.000Z > "$t"
  (
    sleep 0.2
    {
      entry_text 2026-10-31T10:05:10.000Z "$PHRASE"
      entry_tool_use 2026-10-31T10:05:10.000Z toolu_same 'ls'
    } >> "$t"
  ) &
  writer=$!
  expect_allow "phrase written after the hook started" "$(payload "$t" toolu_same)"
  wait "$writer"

  entry_boundary 2026-10-31T10:05:00.000Z > "$t"
  (
    sleep 0.5
    {
      entry_text 2026-10-31T10:05:10.000Z 'Running ls.'
      entry_tool_use 2026-10-31T10:05:10.000Z toolu_other 'ls'
    } >> "$t"
  ) &
  writer=$!
  expect_deny "late write without the phrase" "$(payload "$t" toolu_other)"
  wait "$writer"
  pass "the gate waits for the current message to reach the transcript before it decides"
}

test_recorded_phrase_skips_the_wait() {
  local t="$TMP_ROOT/recorded.jsonl" start elapsed
  {
    entry_boundary 2026-10-31T10:05:00.000Z
    entry_text 2026-10-31T10:05:10.000Z "$PHRASE"
  } > "$t"
  start=$(now_ms)
  expect_allow "phrase already recorded, call not yet written" "$(payload "$t" toolu_unwritten)"
  elapsed=$(( $(now_ms) - start ))
  [ "$elapsed" -lt 1000 ] || fail "a recorded phrase must allow without the 1-second wait, took ${elapsed}ms"
  pass "Bash calls after a recorded phrase do not wait for the transcript"
}

test_out_of_scope_sessions_are_inert() {
  local t="$TMP_ROOT/inert.jsonl" child="$TMP_ROOT/child" rc=0
  entry_boundary 2026-10-31T10:05:00.000Z > "$t"

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
  local t="$TMP_ROOT/registered.jsonl" cmd rc denied=0 p
  entry_boundary 2026-10-31T10:05:00.000Z > "$t"
  p=$(payload "$t")
  while IFS= read -r cmd; do
    rc=0
    printf '%s' "$p" \
      | env -u NO_MISTAKES_GATE -u GROK_AGENT -u GROK_HOOK_EVENT CLAUDE_PROJECT_DIR="$ROOT" \
        FM_ROOT_OVERRIDE="$PRIMARY" FM_HOME="$PRIMARY" FM_STATE_OVERRIDE="$STATE" \
        bash -c "$cmd" > "$OUT" 2> "$ERR" || rc=$?
    if [ "$rc" -eq 2 ] && [ "$(cat "$ERR")" = "$REQUIREMENT" ]; then
      denied=$((denied + 1))
    fi
  done < <(jq -r '.hooks.PreToolUse[] | select(.matcher == "Bash") | .hooks[].command' "$ROOT/.claude/settings.json")
  assert_equals 1 "$denied" "exactly one tracked Claude Bash hook must deny with the requirement"
  pass "the tracked Claude Bash PreToolUse registration runs the gate"
}

fm_test_run \
  test_session_without_compaction_is_allowed \
  test_compaction_without_phrase_is_denied \
  test_phrase_after_compaction_allows \
  test_phrase_before_compaction_does_not_count \
  test_phrase_outside_assistant_text_does_not_count \
  test_phrase_in_same_message_waits_for_transcript_write \
  test_recorded_phrase_skips_the_wait \
  test_out_of_scope_sessions_are_inert \
  test_unusable_transport_fails_open \
  test_tracked_claude_bash_registration_runs_gate
