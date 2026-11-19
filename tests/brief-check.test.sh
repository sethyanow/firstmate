#!/usr/bin/env bash
# The brief check at its two call sites and on its own.
#
# The call sites run against fake tmux worlds with no key and no model, so no
# network is touched. The direct runs point the check at a local stub through
# --questions, with a fake honcho, so the wire behavior is real HTTP.
set -u

# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"
# shellcheck source=bin/fm-timeout-lib.sh
. "$ROOT/bin/fm-timeout-lib.sh"

TMP_ROOT=$(fm_test_tmproot brief-check)
CHECK="$ROOT/bin/fm_brief_check.py"
SKIP_PREFIX='brief-check: skipped: '
FAKE_KEY='sk-or-fake-key-for-the-leak-search'
STUB_PID=
trap '[ -z "$STUB_PID" ] || kill "$STUB_PID" 2>/dev/null; fm_test_cleanup' EXIT

# A python3 that notes each run of the check and then behaves normally, so "the
# check did not run" is observed rather than inferred from silence.
add_python_probe() {  # <fakebin>
  local real
  real=$(command -v python3)
  cat > "$1/python3" <<SH
#!/usr/bin/env bash
case "\$*" in *fm_brief_check.py*) echo ran >> "\$FM_TEST_CHECK_LOG" ;; esac
exec "$real" "\$@"
SH
  chmod +x "$1/python3"
}

check_runs() {  # <log>
  if [ -f "$1" ]; then wc -l < "$1" | tr -d ' '; else echo 0; fi
}

skip_lines() {  # <text>
  printf '%s\n' "$1" | grep -c "^$SKIP_PREFIX"
}

# --- fm-send call site -------------------------------------------------------

SEND_DIR=
SEND_HOME=
SEND_FAKE=
SEND_LOG=

new_send_world() {  # <name>
  SEND_DIR="$TMP_ROOT/send-$1"
  SEND_HOME="$SEND_DIR/home"
  mkdir -p "$SEND_HOME/state" "$SEND_HOME/config" "$SEND_DIR/user-home"
  SEND_FAKE=$(make_stubs "$SEND_DIR")
  SEND_LOG="$SEND_DIR/check.log"
  add_python_probe "$SEND_FAKE"
  fm_write_secondmate_meta "$SEND_HOME/state/domain.meta" "$SEND_HOME" "sess:fm-domain"
  fm_write_meta "$SEND_HOME/state/worker.meta" "window=sess:fm-worker" "endpoint_task_id=worker" \
    "worktree=$SEND_DIR" "project=$SEND_DIR" "harness=echo" "kind=ship"
}

run_send() {  # <fm-send args...>
  env -u OPENROUTER_API_KEY -u CLAUDE_CODE_SESSION_ID -u FM_PENDING_REPLY_EXISTING_CORR \
    PATH="$SEND_FAKE:$PATH" HOME="$SEND_DIR/user-home" FM_ROOT_OVERRIDE="$SEND_HOME" FM_HOME="$SEND_HOME" \
    FM_SEND_SETTLE=0 FM_SEND_LOG="$SEND_DIR/typed.log" FM_TEST_CHECK_LOG="$SEND_LOG" \
    "$ROOT/bin/fm-send.sh" "$@" 2>&1
}

run_send_closed_stderr() {  # <fm-send args...>
  env -u OPENROUTER_API_KEY -u CLAUDE_CODE_SESSION_ID -u FM_PENDING_REPLY_EXISTING_CORR \
    PATH="$SEND_FAKE:$PATH" HOME="$SEND_DIR/user-home" FM_ROOT_OVERRIDE="$SEND_HOME" FM_HOME="$SEND_HOME" \
    FM_SEND_SETTLE=0 FM_SEND_LOG="$SEND_DIR/typed.log" FM_TEST_CHECK_LOG="$SEND_LOG" \
    "$ROOT/bin/fm-send.sh" "$@" 2>&-
}

test_send_no_flag_runs_nothing() {
  local out status
  new_send_world no-flag
  out=$(run_send fm-domain 'audit the build')
  status=$?
  expect_code 0 "$status" "send with no flag should succeed: $out"
  assert_equals 0 "$(check_runs "$SEND_LOG")" "with no flag the check must not run"
  assert_not_contains "$out" 'brief-check:' "with no flag the sender should see nothing from the check"
  assert_absent "$SEND_HOME/state/brief-check" "with no flag the check should log nothing"
  assert_grep 'audit the build' "$SEND_HOME/state/domain.inbox/001.msg" "the message should be delivered"
  pass "fm-send: no flag means the check does not run"
}

test_send_flag_without_key_skips_and_delivers() {
  local out status
  new_send_world no-key
  : > "$SEND_HOME/config/brief-check"
  out=$(run_send fm-domain 'audit the build')
  status=$?
  expect_code 0 "$status" "send with the flag and no key should succeed: $out"
  assert_equals 1 "$(check_runs "$SEND_LOG")" "with the flag a new marked message runs the check"
  assert_equals 1 "$(skip_lines "$out")" "no key should print one skip line: $out"
  assert_contains "$out" "${SKIP_PREFIX}no OPENROUTER_API_KEY" "the skip line should name the reason"
  assert_grep 'audit the build' "$SEND_HOME/state/domain.inbox/001.msg" "the message should be delivered"
  pass "fm-send: flag with no key prints one skip line and delivers the message"
}

test_send_check_failure_keeps_delivery() {
  local out status real
  new_send_world broken
  : > "$SEND_HOME/config/brief-check"
  real=$(command -v python3)
  cat > "$SEND_FAKE/python3" <<SH
#!/usr/bin/env bash
case "\$*" in *fm_brief_check.py*) echo noise >&2; exit 7 ;; esac
exec "$real" "\$@"
SH
  out=$(run_send fm-domain 'audit the build')
  status=$?
  expect_code 0 "$status" "a failing check should not fail the send: $out"
  assert_equals 1 "$(skip_lines "$out")" "a failing check should print one skip line: $out"
  assert_not_contains "$out" 'noise' "the check's own stderr should not reach the sender"
  assert_grep 'audit the build' "$SEND_HOME/state/domain.inbox/001.msg" "the message should be delivered"
  pass "fm-send: a check that dies prints one skip line and delivers the message"
}

test_send_closed_stderr_keeps_delivery() {
  local without with
  new_send_world closed-stderr-no-flag
  run_send_closed_stderr fm-domain 'audit the build'
  without=$?
  new_send_world closed-stderr-flag
  : > "$SEND_HOME/config/brief-check"
  run_send_closed_stderr fm-domain 'audit the build'
  with=$?
  assert_equals "$without" "$with" "a closed stderr should give the same exit with the flag as without it"
  assert_grep 'audit the build' "$SEND_HOME/state/domain.inbox/001.msg" "the message should be delivered"
  pass "fm-send: with a closed stderr the flag changes neither the exit nor the delivery"
}

test_send_exclusions() {
  local out
  new_send_world exclusions
  : > "$SEND_HOME/config/brief-check"

  out=$(run_send fm-worker 'steer the worker')
  assert_grep 'steer the worker' "$SEND_HOME/state/worker.inbox/001.msg" "the worker steer should be delivered: $out"
  assert_equals 0 "$(check_runs "$SEND_LOG")" "a message to a worker must not run the check"

  # A refused resend still shows that the check stayed out of the way.
  out=$(FM_ROOT_OVERRIDE="$SEND_HOME" PATH="$SEND_FAKE:$PATH" HOME="$SEND_DIR/user-home" FM_HOME="$SEND_HOME" \
    FM_SEND_SETTLE=0 FM_SEND_LOG="$SEND_DIR/typed.log" FM_TEST_CHECK_LOG="$SEND_LOG" \
    FM_PENDING_REPLY_EXISTING_CORR=abc123 "$ROOT/bin/fm-send.sh" fm-domain 'resend' 2>&1)
  assert_equals 0 "$(check_runs "$SEND_LOG")" "a resend must not run the check: $out"

  out=$(run_send fm-domain --fire-and-forget 0123456789abcdef 'a recovery notice')
  assert_grep 'a recovery notice' "$SEND_HOME/state/domain.inbox/001.msg" "the notice should be delivered: $out"
  assert_equals 0 "$(check_runs "$SEND_LOG")" "a fire-and-forget notice must not run the check: $out"

  out=$(run_send fm-domain 'CONFIG_REREAD: /some/instructions')
  assert_equals 0 "$(check_runs "$SEND_LOG")" "a fixed config notice must not run the check: $out"

  # shellcheck source=/dev/null
  . "$ROOT/bin/fm-secondmate-nudge-lib.sh"
  out=$(run_send fm-domain "$FM_SECOND_MATE_NUDGE_MESSAGE")
  assert_equals 0 "$(check_runs "$SEND_LOG")" "the fixed update notice must not run the check: $out"
  pass "fm-send: workers, resends, and fixed notices do not run the check"
}

# --- fm-spawn call site ------------------------------------------------------

SPAWN_DIR=
SPAWN_HOME=
SPAWN_WT=
SPAWN_PROJ=
SPAWN_FAKE=
SPAWN_LOG=
LAUNCH_LOG=

new_spawn_world() {  # <name> <task-id>
  SPAWN_DIR="$TMP_ROOT/spawn-$1"
  SPAWN_HOME="$SPAWN_DIR/home"
  SPAWN_PROJ="$SPAWN_DIR/project"
  SPAWN_WT="$SPAWN_DIR/wt"
  SPAWN_LOG="$SPAWN_DIR/check.log"
  LAUNCH_LOG="$SPAWN_DIR/launch.log"
  SPAWN_FAKE=$(fm_test_make_spawn_fakebin "$SPAWN_DIR/fake")
  add_python_probe "$SPAWN_FAKE"
  fm_test_spawn_home "$SPAWN_HOME" codex
  fm_git_worktree "$SPAWN_PROJ" "$SPAWN_WT" "wt-$1"
  fm_test_spawn_brief "$SPAWN_HOME" "$2"
}

run_spawn() {  # <fm-spawn args...>
  : > "$LAUNCH_LOG"
  (
    unset OPENROUTER_API_KEY CLAUDE_CODE_SESSION_ID
    export FM_FAKE_LAUNCH_LOG="$LAUNCH_LOG" FM_TEST_CHECK_LOG="$SPAWN_LOG"
    fm_test_run_spawn "$SPAWN_HOME" "$SPAWN_WT" "$SPAWN_FAKE" "$@"
  )
}

test_spawn_no_flag_runs_nothing() {
  local out status
  new_spawn_world no-flag no-flag-a1
  out=$(run_spawn no-flag-a1 "$SPAWN_PROJ" --mode no-mistakes --yolo off)
  status=$?
  expect_code 0 "$status" "spawn with no flag should succeed: $out"
  assert_equals 0 "$(check_runs "$SPAWN_LOG")" "with no flag the check must not run"
  assert_not_contains "$out" 'brief-check:' "with no flag the sender should see nothing from the check"
  [ -s "$LAUNCH_LOG" ] || fail "spawn with no flag should launch: $out"
  pass "fm-spawn: no flag means the check does not run"
}

test_spawn_flag_without_key_skips_and_launches() {
  local out status
  new_spawn_world no-key no-key-a1
  : > "$SPAWN_HOME/config/brief-check"
  out=$(run_spawn no-key-a1 "$SPAWN_PROJ" --mode no-mistakes --yolo off)
  status=$?
  expect_code 0 "$status" "spawn with the flag and no key should succeed: $out"
  assert_equals 1 "$(check_runs "$SPAWN_LOG")" "a fresh launch runs the check"
  assert_equals 1 "$(skip_lines "$out")" "no key should print one skip line: $out"
  [ -s "$LAUNCH_LOG" ] || fail "spawn with the flag and no key should launch: $out"
  pass "fm-spawn: flag with no key prints one skip line and launches"
}

test_spawn_relaunch_runs_nothing() {
  local out status
  new_spawn_world relaunch relaunch-a1
  : > "$SPAWN_HOME/config/brief-check"
  out=$(run_spawn relaunch-a1 "$SPAWN_PROJ" --mode no-mistakes --yolo off)
  status=$?
  expect_code 0 "$status" "the fresh spawn should succeed: $out"
  assert_equals 1 "$(check_runs "$SPAWN_LOG")" "the fresh launch runs the check once"
  : > "$SPAWN_HOME/config/memory-check-required"
  printf 'invalid config that a relaunch must not read' > "$SPAWN_HOME/config/memory-check.json"
  # The agent has exited, so its pane holds a bare shell that a relaunch may reuse.
  mv "$SPAWN_FAKE/tmux" "$SPAWN_FAKE/tmux.spawn"
  cat > "$SPAWN_FAKE/tmux" <<SH
#!/usr/bin/env bash
case "\$*" in *pane_current_command*) echo zsh; exit 0 ;; esac
exec "$SPAWN_FAKE/tmux.spawn" "\$@"
SH
  chmod +x "$SPAWN_FAKE/tmux"
  out=$(run_spawn relaunch-a1 --relaunch)
  status=$?
  expect_code 0 "$status" "the relaunch should succeed: $out"
  [ -s "$LAUNCH_LOG" ] || fail "the relaunch should launch: $out"
  assert_equals 1 "$(check_runs "$SPAWN_LOG")" "a relaunch must not run the check"
  pass "fm-spawn: a relaunch does not run the check"
}

test_spawn_secondmate_runs_nothing() {
  local out status mate
  new_spawn_world secondmate secondmate-a1
  : > "$SPAWN_HOME/config/brief-check"
  : > "$SPAWN_HOME/config/memory-check-required"
  printf 'invalid config that a secondmate must not read' > "$SPAWN_HOME/config/memory-check.json"
  mate="$SPAWN_DIR/mate-home"
  mkdir -p "$mate/bin" "$mate/data"
  printf '# Firstmate\n' > "$mate/AGENTS.md"
  printf 'secondmate-a1\n' > "$mate/.fm-secondmate-home"
  printf 'charter\n' > "$mate/data/charter.md"
  printf '%s\n' 'projects/' 'state/' 'data/' 'config/' '.no-mistakes/' > "$mate/.gitignore"
  git -C "$mate" init -q -b main
  out=$(run_spawn secondmate-a1 "$mate" --secondmate)
  status=$?
  expect_code 0 "$status" "the secondmate launch should succeed: $out"
  [ -s "$LAUNCH_LOG" ] || fail "the secondmate should launch: $out"
  assert_equals 0 "$(check_runs "$SPAWN_LOG")" "a secondmate launch must not run the check"
  pass "fm-spawn: a secondmate launch does not run the check"
}

test_spawn_refusal_runs_nothing() {
  local out status
  new_spawn_world refused refused-a1
  : > "$SPAWN_HOME/config/brief-check"
  printf '# Task\n## Captain'\''s intent\n{TASK}\n\n## Firstmate spec\n{FIRSTMATE_SPEC}\n' \
    > "$SPAWN_HOME/data/refused-a1/brief.md"
  out=$(run_spawn refused-a1 "$SPAWN_PROJ" --mode no-mistakes --yolo off)
  status=$?
  [ "$status" -ne 0 ] || fail "an unfilled brief should be refused: $out"
  assert_equals 0 "$(check_runs "$SPAWN_LOG")" "a refused spawn must not run the check"
  pass "fm-spawn: a refused launch does not run the check"
}

# --- direct runs against a local stub ----------------------------------------

STUB_PORT=
DIRECT_DIR=

have_tomllib() {
  python3 -c 'import tomllib' 2>/dev/null
}

start_stub() {
  [ -z "$STUB_PORT" ] || return 0
  local dir="$TMP_ROOT/stub" i
  mkdir -p "$dir"
  cat > "$dir/stub.py" <<'PY'
"""The decision endpoint's contract. Its paths choose how it answers."""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

root = Path(sys.argv[1])
key = sys.argv[2]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, payload, headers=()):
        data = json.dumps(payload).encode()
        self.send_response(code)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size).decode()
        got = self.headers.get("Authorization", "")
        with open(root / "requests.jsonl", "a") as fh:
            fh.write(json.dumps({"path": self.path, "key_sent": got == f"Bearer {key}", "any_auth": bool(got), "body": body}) + "\n")
        if self.path == "/memory":
            self.send(200, {"answer": "1. The approved option is a local prototype, without publication."})
        elif self.path == "/memory-absent":
            self.send(200, {"answer": "I don't have the review's findings in the available record."})
        elif self.path == "/memory-notes":
            self.send(200, {"answer": "- He wants plain words.\n- He wants a test."})
        elif self.path == "/memory-list":
            self.send(200, {"results": [{"text": "He keeps the check generic.", "score": 0.9},
                                        {"text": "He wants plain words.", "score": 0.8},
                                        {"summary": "an item with no text", "score": 0.1}]})
        elif self.path in ("/ok", "/unlocated"):
            questions = json.loads(body)["questions"]
            answers = {}
            for qid, q in questions.items():
                if q["type"] == "noul":
                    answers[qid] = {"type": "noul", "noul": 0.9}
                else:
                    options = list(q["criteria"])
                    choice = options[0] if self.path == "/ok" else "none"
                    probabilities = {o: float(o == choice) for o in options}
                    answers[qid] = {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": 0.5}
            self.send(200, {"model": "stub", "answers": answers, "usage": {"input_tokens": 1234, "output_tokens": 0}})
        elif self.path == "/clean":
            questions = json.loads(body)["questions"]
            answers = {qid: {"type": "noul", "noul": 0.5} if q["type"] == "noul" else
                       {"type": "choice", "choice": "none", "probabilities": {o: float(o == "none") for o in q["criteria"]}, "confidence": 0.5}
                       for qid, q in questions.items()}
            self.send(200, {"model": "stub", "answers": answers, "usage": {"input_tokens": 1234, "output_tokens": 0}})
        elif self.path == "/echo":
            self.send(401, {"error": f"bad key {key}"})
        elif self.path == "/redirect":
            self.send(302, {}, [("Location", f"http://127.0.0.1:{self.server.server_port}/target")])
        elif self.path == "/malformed":
            self.send(200, {"model": "stub"})
        else:
            self.send(200, {"reached": self.path})

    do_POST = do_GET = handle_any


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
(root / "port.tmp").write_text(str(server.server_port))
(root / "port.tmp").rename(root / "port")
server.serve_forever()
PY
  python3 "$dir/stub.py" "$dir" "$FAKE_KEY" &
  STUB_PID=$!
  for i in $(seq 1 100); do
    [ ! -f "$dir/port" ] || break
    sleep 0.05
  done
  [ -f "$dir/port" ] || fail "the stub endpoint did not start"
  STUB_PORT=$(cat "$dir/port")
}

# A home, a session log holding each record shape, and a fake honcho.
new_direct_world() {  # <name> <endpoint-path>
  DIRECT_DIR="$TMP_ROOT/direct-$1"
  mkdir -p "$DIRECT_DIR/home/config" "$DIRECT_DIR/home/data" "$DIRECT_DIR/home/state" "$DIRECT_DIR/bin" \
    "$DIRECT_DIR/user-home/.claude/projects/some-project"
  cat > "$DIRECT_DIR/bin/honcho" <<'SH'
#!/usr/bin/env bash
echo called >> "${0%/*}/honcho.calls"
[ -z "${FM_TEST_HONCHO_FAIL:-}" ] || { echo 'honcho is down' >&2; exit 3; }
printf '%s\n' '{"response": "1. He wants plain words.\n2. He wants a test."}'
SH
  chmod +x "$DIRECT_DIR/bin/honcho"
  sed "s|^endpoint = .*|endpoint = \"http://127.0.0.1:$STUB_PORT/$2\"|" "$ROOT/bin/fm-brief-check-questions.toml" \
    > "$DIRECT_DIR/questions.toml"
  cat > "$DIRECT_DIR/user-home/.claude/projects/some-project/session-1.jsonl" <<'JSONL'
{"type":"user","origin":{"kind":"human"},"message":{"content":"an older message"}}
{"type":"user","origin":{"kind":"human"},"message":{"content":[{"type":"text","text":"first typed"}]}}
{"type":"queue-operation","operation":"remove","content":"queued words"}
{"attachment":{"type":"queued_command","prompt":"queued words","origin":{"kind":"human"}}}
{"attachment":{"type":"queued_command","prompt":"attached words","origin":{"kind":"human"}}}
{"type":"queue-operation","operation":"enqueue","content":"<task-notification>hook feedback</task-notification>"}
{"attachment":{"type":"queued_command","prompt":"a machine prompt","origin":{"kind":"task-notification"}}}
{"type":"user","origin":{"kind":"human"},"message":{"content":": Firstmate operational input waiting"}}
{"type":"user","origin":{"kind":"human"},"message":{"content":"Firstmate instruction waiting in the inbox"}}
{"type":"queue-operation","operation":"enqueue","content":"Firstmate note waiting in the inbox"}
{"type":"queue-operation","operation":"enqueue","content":"FIRSTMATE SUPERVISION WAKE: signal: a task"}
{"type":"queue-operation","operation":"enqueue","content":"MAIN DIALOG MIRROR (read-only)"}
{"type":"user","origin":{"kind":"human"},"message":{"content":"[fm-from-firstmate]corr=ab a marked message"}}
{"type":"user","message":{"content":"a summary record with no origin"}}
{"type":"assistant","message":{"content":[{"type":"text","text":"Shall I go ahead?"}]}}
JSONL
  printf '# Task\n## Captain'\''s intent\nHis words: "an older message"\n\n## Firstmate spec\nBuild the small thing.\n' > "$DIRECT_DIR/brief.md"
}

# Runs under the caller's OPENROUTER_API_KEY, and FM_TEST_SESSION_ID can name another session.
run_direct() {  # <check args...>
  env PATH="$DIRECT_DIR/bin:$PATH" HOME="$DIRECT_DIR/user-home" FM_HOME="$DIRECT_DIR/home" \
    CLAUDE_CODE_SESSION_ID="${FM_TEST_SESSION_ID:-session-1}" \
    python3 "$CHECK" --questions "$DIRECT_DIR/questions.toml" "$@" 2>&1
}

last_request() {
  tail -n 1 "$TMP_ROOT/stub/requests.jsonl"
}

assert_no_key_anywhere() {  # <output>
  assert_not_contains "$1" "$FAKE_KEY" "the key must not reach the output"
  if [ -d "$DIRECT_DIR/home/state/brief-check" ] && grep -rqF "$FAKE_KEY" "$DIRECT_DIR/home/state/brief-check"; then
    fail "the key must not reach the log"
  fi
}

test_direct_reports_flags() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world flags ok
  local out status request
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md")
  status=$?
  expect_code 0 "$status" "a run with flags should exit 0: $out"
  assert_contains "$out" 'brief-check send: ' "the sender should see the result: $out"
  assert_contains "$out" 'jev ' "the result should name the model"
  assert_contains "$out" 'Widens the ask' "a flag over the floor should reach the sender: $out"
  assert_contains "$out" "L2: ## Captain's intent" "a located flag should show its block: $out"
  assert_contains "$out" '1234 tokens' "the result should show the token count"
  request=$(last_request)
  assert_equals true "$(printf '%s' "$request" | jq -r .key_sent)" "the key should go as the request header"
  grep -q '"input_tokens": 1234' "$DIRECT_DIR"/home/state/brief-check/*-send.json \
    || fail "the log should hold the token count"
  assert_no_key_anywhere "$out"
  pass "direct: flags reach the sender"
}

sent_ask() {
  last_request | jq -r '.body | fromjson | .state.ask'
}

test_direct_ask_is_his_quoted_messages() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world quoted-ask ok
  printf '%s\n' \
    '# Task' \
    "## Captain's intent" \
    'He said: "An Older message" and later "attached words, then more".' \
    'On the board: "pick the second layout".' \
    'Again: "an older message".' \
    '## Firstmate spec' \
    'Do it.' > "$DIRECT_DIR/brief.md"
  local ask
  OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" >/dev/null
  ask=$(sent_ask)
  assert_contains "$ask" 'an older message' "a message of his that the text quotes should be in the ask: $ask"
  assert_contains "$ask" 'attached words, then more' "a quote found in none of his messages should be in the ask as written: $ask"
  assert_contains "$ask" 'pick the second layout' "a board answer should be in the ask as written: $ask"
  assert_not_contains "$ask" 'first typed' "a message the text does not quote should stay out of the ask: $ask"
  pass "direct: the ask is his quoted messages, with other quotes as written"
}

test_direct_ask_without_quotes_is_his_last_messages() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world unquoted-ask ok
  printf '# Task\n## Captain'\''s intent\nthe intent line, short "go" only\n\n## Firstmate spec\nBuild it.\n' > "$DIRECT_DIR/brief.md"
  OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" >/dev/null
  assert_equals "$(printf 'first typed\n\nqueued words\n\nattached words')" "$(sent_ask)" \
    "a text that quotes none of his words should carry his last three typed messages as the ask"
  pass "direct: with no quote the ask is his last three typed messages"
}

test_direct_unquoted_text_gets_no_intent_or_honcho_questions() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world unquoted ok
  local out
  # Quotes outside the task sections are template text, not his words.
  printf '%s\n' '# Task' "## Captain's intent" "main's own cleanup" '## Firstmate spec' 'Build it.' \
    '# Rules' 'Report with: echo "done: ready in the branch" >> the status file' > "$DIRECT_DIR/brief.md"
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md")
  assert_contains "$out" 'No real go for this work' "the go question should still be asked: $out"
  assert_not_contains "$out" 'Widens the ask' "an intent question should not be asked: $out"
  assert_not_contains "$out" 'intent flags with no line' "no intent flag should exist to hide: $out"
  assert_absent "$DIRECT_DIR/bin/honcho.calls" "Honcho should not be asked"
  pass "direct: a text that quotes none of his words gets no intent or Honcho questions"
}

# The home's wake drain, as its firstmate runs it. Prints the drain's stdout.
drain_direct_home() {
  mkdir -p "$DIRECT_DIR/drain-config"
  : > "$DIRECT_DIR/drain-config/supervision-host-off"
  env -u FM_SUPERVISION_ACTOR FM_HOME="$DIRECT_DIR/home" FM_ROOT_OVERRIDE="$DIRECT_DIR/home" \
    FM_STATE_OVERRIDE="$DIRECT_DIR/home/state" FM_CONFIG_OVERRIDE="$DIRECT_DIR/drain-config" \
    "$ROOT/bin/fm-wake-drain.sh" 2>/dev/null
}

test_drain_shows_a_sends_flags_once_without_a_wake() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world notice ok
  local first second
  OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" --to some-mate >/dev/null
  if [ -s "$DIRECT_DIR/home/state/.wake-queue" ]; then
    fail "a flagged send must not queue a wake: $(cat "$DIRECT_DIR/home/state/.wake-queue")"
  fi
  first=$(drain_direct_home)
  assert_contains "$first" 'BRIEF CHECK FLAGS' "the next drain should carry the flags: $first"
  assert_contains "$first" 'to some-mate' "the notice should name the recipient: $first"
  assert_contains "$first" 'Widens the ask' "the notice should hold the shown flags: $first"
  assert_contains "$first" "L2: ## Captain's intent" "the notice should name the flagged block: $first"
  second=$(drain_direct_home)
  assert_not_contains "$second" 'BRIEF CHECK FLAGS' "a later drain should not print the flags again: $second"
  pass "drain: a send's flags show once on the next drain, with no wake of their own"
}

test_drain_shows_nothing_for_a_send_with_no_flag() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world no-notice clean
  local out drained
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md")
  assert_contains "$out" 'brief-check send: 0 flags' "setup: the run should show no flag: $out"
  drained=$(drain_direct_home)
  assert_not_contains "$drained" 'BRIEF CHECK FLAGS' "a send with no flag should leave the drain quiet: $drained"
  pass "drain: a send with no flag leaves nothing for the drain"
}

test_direct_brief_with_many_headings_finishes() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world headings ok
  local out status i
  {
    printf '# Task\n## Captain'\''s intent\nthe intent line\n\n## Firstmate spec\n'
    for i in $(seq 1 30); do printf '### Step %s\nDo step %s.\n' "$i" "$i"; done
  } > "$DIRECT_DIR/brief.md"
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" fm_run_timed 20 env PATH="$DIRECT_DIR/bin:$PATH" HOME="$DIRECT_DIR/user-home" \
    FM_HOME="$DIRECT_DIR/home" CLAUDE_CODE_SESSION_ID=session-1 \
    python3 "$CHECK" --questions "$DIRECT_DIR/questions.toml" --text "$DIRECT_DIR/brief.md" 2>&1)
  status=$?
  expect_code 0 "$status" "a brief with more headings than blocks should finish: $out"
  assert_contains "$out" 'brief-check send: ' "the sender should see the result: $out"
  pass "direct: a brief with more headings than blocks still finishes"
}

test_direct_key_from_the_named_file() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world named-file ok
  local out
  printf 'OTHER=1\nexport OPENROUTER_API_KEY="%s"\n' "$FAKE_KEY" > "$DIRECT_DIR/user-home/keys.env"
  # shellcheck disable=SC2088
  printf '~/keys.env\nany later line is ignored\n' > "$DIRECT_DIR/home/config/brief-check"
  out=$(unset OPENROUTER_API_KEY; run_direct --text "$DIRECT_DIR/brief.md")
  assert_contains "$out" 'brief-check send: ' "the named file should supply the key: $out"
  assert_equals true "$(last_request | jq -r .key_sent)" "the key from the file should go as the request header"
  assert_no_key_anywhere "$out"
  pass "direct: the key can come from the file the flag names"
}

test_direct_hides_only_unlocated_intent_flags() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world unlocated unlocated
  local out log
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" --to some-task)
  assert_not_contains "$out" 'Widens the ask' "an unlocated flag from a question that locates should not show: $out"
  assert_contains "$out" 'No real go for this work' "a flag from a question with no locator should still show: $out"
  assert_contains "$out" 'line: not located' "a shown flag with no line should say so: $out"
  assert_contains "$out" 'Settles an open question by guess' "an unlocated process flag should still show: $out"
  assert_contains "$out" 'of them intent flags with no line' "the hidden flags should be counted: $out"
  log=$(cat "$DIRECT_DIR"/home/state/brief-check/*-send.json)
  assert_equals true "$(printf '%s' "$log" | jq '[.flags[].label] | index("Widens the ask") != null')" \
    "the log should keep the hidden flag"
  assert_equals some-task "$(printf '%s' "$log" | jq -r .to)" "the log should name the recipient"
  pass "direct: an unlocated flag is hidden only when it is an intent flag"
}

test_direct_read_error_prints_the_message() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  local out status key
  for key in "$FAKE_KEY" 'invalid key with spaces' 'key\with-backslash' "key'\\with-quote"; do
    out=$(OPENROUTER_API_KEY="$key" FM_HOME="$TMP_ROOT" PYTHONIOENCODING=ascii python3 "$CHECK" \
      --text "$TMP_ROOT/missing-\"café-$key.md" 2>&1)
    status=$?
    expect_code 0 "$status" "an unreadable brief should exit 0 with ASCII stdout: $out"
    assert_contains "$out" "${SKIP_PREFIX}FileNotFoundError: " "the skip line should describe the read error: $out"
    assert_contains "$out" 'missing-"caf\xe9-[key].md' "the skip line should escape and redact the missing file: $out"
    assert_not_contains "$out" "$key" "the key must not reach the output"
  done
  out=$(OPENROUTER_API_KEY='invalid key with spaces' FM_HOME="$TMP_ROOT" python3 "$CHECK" --text - </dev/null 2>&1)
  expect_code 0 "$?" "a rejected key should exit 0: $out"
  assert_equals "${SKIP_PREFIX}no OPENROUTER_API_KEY" "$out" "a rejected key should skip the check"
  pass "direct: an unreadable brief prints the error message without the key"
}

test_direct_failures_skip_without_the_key() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  local out status path
  for path in echo malformed; do
    new_direct_world "fail-$path" "$path"
    out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md")
    status=$?
    expect_code 0 "$status" "a failed answer ($path) should exit 0: $out"
    assert_equals 1 "$(skip_lines "$out")" "a failed answer ($path) should print one skip line: $out"
    assert_equals 1 "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "a failed answer ($path) should print nothing else: $out"
    assert_no_key_anywhere "$out"
  done
  new_direct_world fail-honcho ok
  out=$(FM_TEST_HONCHO_FAIL=1 OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md")
  status=$?
  expect_code 0 "$status" "a failed Honcho call should exit 0: $out"
  assert_equals 1 "$(skip_lines "$out")" "a failed Honcho call should print one skip line: $out"
  pass "direct: a failed answer prints one skip line and never the key"
}

test_direct_key_in_the_text_is_not_sent() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world key-in-text ok
  local out
  printf '# Task\n## Captain'\''s intent\nuse %s here\n\n## Firstmate spec\nBuild it.\n' "$FAKE_KEY" > "$DIRECT_DIR/brief.md"
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md")
  assert_contains "$out" 'brief-check send: ' "the check should still run: $out"
  assert_not_contains "$(last_request | jq -r .body)" "$FAKE_KEY" "the key must not go in the request body"
  assert_no_key_anywhere "$out"
  pass "direct: the key is removed from text that happens to hold it"
}

test_direct_refuses_a_redirect() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world redirect redirect
  local out status
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md")
  status=$?
  expect_code 0 "$status" "a redirect should exit 0: $out"
  assert_equals 1 "$(skip_lines "$out")" "a redirect should print one skip line: $out"
  assert_no_grep '"path": "/target"' "$TMP_ROOT/stub/requests.jsonl" "the redirect target must never be reached"
  pass "direct: a redirect is refused, so the key cannot follow it"
}

test_direct_no_session_log_skips() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world no-session ok
  local out status
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" FM_TEST_SESSION_ID=unknown-session run_direct --text "$DIRECT_DIR/brief.md")
  status=$?
  expect_code 0 "$status" "no session log should exit 0: $out"
  assert_equals "${SKIP_PREFIX}no session log" "$out" "no session log should print one skip line"
  pass "direct: no session log prints one skip line"
}

collect_memory() {  # <home> <task> [extra query]
  printf '{"backends":[{"name":"archive","url":"http://127.0.0.1:%s/memory","method":"POST","body":{"query":"{query}","context":"{context}"},"answer_path":"/answer"}]}\n' \
    "$STUB_PORT" > "$1/config/memory-check.json"
  [ -f "$1/exchange.txt" ] || printf 'Option 4A: a local prototype.\nOption 4B: publish it.\nReply: 4A go\n' > "$1/exchange.txt"
  FM_HOME="$1" FM_DATA_OVERRIDE='' FM_CONFIG_OVERRIDE='' python3 "$ROOT/bin/fm_memory_check.py" \
    run "$2" --context "$1/exchange.txt" --query 'previous brief decisions' ${3:+--query "$3"}
}

test_spawn_memory_gate() {
  start_stub
  new_spawn_world memory-gate memory-gate-a1
  local out check_id
  check_id=$(collect_memory "$SPAWN_HOME" memory-gate-a1)
  : > "$SPAWN_HOME/config/memory-check-required"
  if out=$(run_spawn memory-gate-a1 "$SPAWN_PROJ" --mode no-mistakes --yolo off); then
    fail "a fresh launch without an id must be refused: $out"
  fi
  assert_contains "$out" 'memory check required' "the refusal must explain the missing id"
  assert_absent "$SPAWN_HOME/state/memory-gate-a1.meta" "refusal must not publish a task"
  [ ! -s "$LAUNCH_LOG" ] || fail "refusal must not launch an agent"
  printf '\nMemory check: %s\n' "$check_id" >> "$SPAWN_HOME/data/memory-gate-a1/brief.md"
  out=$(run_spawn memory-gate-a1 "$SPAWN_PROJ" --mode no-mistakes --yolo off)
  expect_code 0 "$?" "matching task evidence must admit the launch: $out"
  [ -s "$LAUNCH_LOG" ] || fail "admitted spawn must launch"
  pass "fm-spawn: the opt-in gate refuses missing evidence and admits a matching check"
}

test_spawn_memory_gate_handles_dash_task_ids() {
  local task_id out
  for task_id in -x --help; do
    new_spawn_world "memory-gate-$task_id" "$task_id"
    printf '%s\n' '{"backends":[{"name":"archive","url":"https://memory.example/read","method":"GET","answer_path":""}]}' \
      > "$SPAWN_HOME/config/memory-check.json"
    : > "$SPAWN_HOME/config/memory-check-required"
    if out=$(run_spawn --mode no-mistakes --yolo off "$task_id" "$SPAWN_PROJ"); then
      fail "a dash-prefixed task without evidence must be refused: $out"
    fi
    assert_contains "$out" 'memory check required' "the task id must reach evidence validation: $out"
    assert_absent "$SPAWN_HOME/state/$task_id.meta" "refusal must not publish a task"
    [ ! -s "$LAUNCH_LOG" ] || fail "refusal must not launch an agent"
  done
  pass "fm-spawn: dash-prefixed task ids reach the required memory gate"
}

test_direct_saved_exchange_and_feedback() {
  start_stub
  new_direct_world memory ok
  local out check_id flag_file run_id disposition
  check_id=$(collect_memory "$DIRECT_DIR/home" memory-a1)
  printf '# Task\n## Captain'\''s intent\nBuild a local prototype.\n## Firstmate spec\nKeep it local.\nMemory check: %s\n' \
    "$check_id" > "$DIRECT_DIR/brief.md"
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" FM_TEST_SESSION_ID=unknown-session run_direct \
    --text "$DIRECT_DIR/brief.md" --task memory-a1)
  assert_contains "$out" 'brief-check send:' "saved evidence must work without a Claude session: $out"
  assert_contains "$out" 'memory check' "the summary must name the source"
  assert_equals "$(cat "$DIRECT_DIR/home/exchange.txt")" "$(sent_ask)" "the whole exchange must reach the judge"
  assert_equals "$check_id" "$(jq -r .id "$DIRECT_DIR/home/data/memory-a1/memory-check.json")" "the check id must survive"
  assert_contains "$(last_request | jq -r .body)" 'without publication' "backend evidence must reach the judge"
  assert_absent "$DIRECT_DIR/bin/honcho.calls" "a saved check must replace the old memory call"
  flag_file=$(printf '%s\n' "$DIRECT_DIR/home/data/memory-a1/brief-check/"*.json)
  run_id=$(jq -r .run_id "$flag_file")
  for disposition in acted-on dropped; do
    FM_HOME="$DIRECT_DIR/home" FM_DATA_OVERRIDE='' python3 "$ROOT/bin/fm_brief_check.py" \
      feedback memory-a1 --run "$run_id" --flag widened --disposition "$disposition" \
      --reason 'Reviewed the approved scope.' || fail "feedback command failed"
  done
  assert_equals 'acted-on dropped' "$(jq -r .disposition "$DIRECT_DIR/home/data/memory-a1/brief-check-feedback.jsonl" | paste -sd ' ' -)" \
    "feedback must retain each decision"
  if out=$(FM_HOME="$DIRECT_DIR/home" FM_DATA_OVERRIDE='' python3 "$ROOT/bin/fm_brief_check.py" \
    feedback memory-a1 --run "$run_id" --flag invented --disposition dropped --reason noise 2>&1); then
    fail "feedback for a nonexistent flag must fail"
  fi
  assert_contains "$out" 'unknown flag' "a mistyped flag must be explained"
  pass "direct: saved context replaces chat and memory lookup, and flags accept audited feedback"
}

test_direct_absent_recall_has_no_expectation_questions() {
  start_stub
  new_direct_world absent-recall ok
  local home="$DIRECT_DIR/home" out
  printf '{"backends":[{"name":"archive","url":"http://127.0.0.1:%s/memory-absent","method":"POST","answer_path":"/answer"}]}\n' \
    "$STUB_PORT" > "$home/config/memory-check.json"
  printf 'Apply the review corrections.\n' > "$home/exchange.txt"
  FM_HOME="$home" python3 "$ROOT/bin/fm_memory_check.py" run absent-a1 --context "$home/exchange.txt" --query 'review findings' >/dev/null \
    || fail "the no-record answer must still be saved"
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" --task absent-a1)
  assert_contains "$out" 'brief-check send:' "the check must complete: $out"
  assert_equals 0 "$(last_request | jq '.body | fromjson | [.questions | keys[] | select(startswith("x_"))] | length')" \
    "a no-record answer must not create expectation questions"
  pass "direct: absent recall creates no expectation questions"
}

test_direct_steer_ignores_the_launch_check() {
  start_stub
  new_direct_world steer ok
  local out
  collect_memory "$DIRECT_DIR/home" steer-a1 >/dev/null
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" --to steer-a1)
  assert_equals 'an older message' "$(sent_ask)" "a steer should be judged against the words it quotes: $out"
  assert_not_contains "$out" 'memory check' "a steer should not use the launch's saved check: $out"
  assert_absent "$DIRECT_DIR/home/data/steer-a1/brief-check" "a steer should leave no flag record in the launch's task"
  pass "direct: a steer to a task with a saved check is judged on its own ask"
}

test_direct_list_answers_become_points() {
  start_stub
  new_direct_world list-points ok
  local home="$DIRECT_DIR/home" points
  printf '{"backends":[%s,%s]}\n' \
    "{\"name\":\"notes\",\"url\":\"http://127.0.0.1:$STUB_PORT/memory-notes\",\"method\":\"POST\",\"answer_path\":\"/answer\"}" \
    "{\"name\":\"lists\",\"url\":\"http://127.0.0.1:$STUB_PORT/memory-list\",\"method\":\"POST\",\"answer_path\":\"/results\",\"item_path\":\"/text\"}" \
    > "$home/config/memory-check.json"
  printf 'Reply: go\n' > "$home/exchange.txt"
  FM_HOME="$home" python3 "$ROOT/bin/fm_memory_check.py" run list-a1 --context "$home/exchange.txt" --query 'his rules' >/dev/null \
    || fail "the memory check should collect both backends"
  OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" --task list-a1 >/dev/null
  points=$(last_request | jq -r '.body | fromjson | .state.points | [.[]] | sort | join("|")')
  assert_contains "$points" 'He keeps the check generic.' "selected array text must reach the judge"
  assert_contains "$points" 'He wants plain words.' "text answer points must reach the judge"
  pass "direct: a list answer gives one point per item"
}

# Runs a launch check of task <task> whose saved exchange is <exchange>. Prints the screen.
run_exchange_check() {  # <task> <exchange>
  local home="$DIRECT_DIR/home"
  printf '%s\n' "$2" > "$home/exchange.txt"
  collect_memory "$home" "$1" >/dev/null || fail "the memory check should save the exchange"
  OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" --task "$1"
}

test_direct_lists_exchange_misquotes() {
  start_stub
  new_direct_world misquote ok
  local out
  out=$(run_exchange_check misquote-a1 'He said: "an older message" and "queued words, then more"')
  assert_contains "$out" 'not found in his messages: "queued words, then more"' "a misquote should be listed: $out"
  assert_not_contains "$out" '"an older message"' "a quote he typed should not be listed: $out"
  assert_equals '"queued words, then more"' \
    "$(jq -r '[.exchange_quotes[] | select(.found | not) | "\"" + .quote + "\""] | join(",")' "$DIRECT_DIR"/home/state/brief-check/*-send.json)" \
    "the log should record which quotes were not found"
  out=$(FM_TEST_SESSION_ID=unknown-session run_exchange_check misquote-a2 'He said: "an older message"')
  assert_contains "$out" 'no session log' "with no session log a quote cannot be checked, and should say so: $out"
  pass "direct: a quote in the saved exchange that he never typed is listed"
}

test_direct_checks_card_quotes_against_the_card() {
  start_stub
  new_direct_world card ok
  local out home="$DIRECT_DIR/home"
  cat > "$DIRECT_DIR/bin/owner-replies" <<'SH'
#!/usr/bin/env bash
[ "$1" = c1 ] || { echo "no card $1" >&2; exit 2; }
printf 'agreed, draft it up\nand keep it short\n'
SH
  chmod +x "$DIRECT_DIR/bin/owner-replies"
  printf '%s\n' 'On card [card:c1]: "agreed, draft it up" then "an older message". On [card:c9]: "something else entirely"' \
    > "$home/exchange.txt"
  collect_memory "$home" card-a1 >/dev/null || fail "the memory check should save the exchange"
  jq '.quote_sources = {card: {command: ["owner-replies", "{id}"]}}' "$home/config/memory-check.json" > "$home/config/next.json"
  mv "$home/config/next.json" "$home/config/memory-check.json"
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" --task card-a1)
  assert_not_contains "$out" '"agreed, draft it up"' "a quote from the card's thread should not be listed: $out"
  assert_contains "$out" 'not found in card:c1: "an older message"' "a card quote should be checked against that card, not the session: $out"
  assert_contains "$out" 'card:c9' "a card that cannot be read should be named: $out"
  assert_contains "$out" '"something else entirely"' "a quote from an unreadable card should be listed: $out"
  pass "direct: a quote tagged with a configured source is checked against that source"
}

test_direct_saved_check_summary_comes_from_the_record() {
  start_stub
  new_direct_world summary ok
  local out check_id
  check_id=$(collect_memory "$DIRECT_DIR/home" summary-a1 'brief rules')
  printf '# Task\n## Captain'\''s intent\nBuild a local prototype.\nMemory check: %s\n' "$check_id" > "$DIRECT_DIR/brief.md"
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct --text "$DIRECT_DIR/brief.md" --task summary-a1)
  assert_contains "$out" 'Memory check: 2 queries, 1 backend, 0 errors' "the sender block should summarize the saved check: $out"
  assert_not_contains "$out" 'Honcho asked' "a saved check should replace the Honcho call count: $out"
  pass "direct: with a saved check the sender block reports the check, not Honcho calls"
}

fm_test_run \
  test_send_no_flag_runs_nothing \
  test_send_flag_without_key_skips_and_delivers \
  test_send_check_failure_keeps_delivery \
  test_send_closed_stderr_keeps_delivery \
  test_send_exclusions \
  test_spawn_no_flag_runs_nothing \
  test_spawn_flag_without_key_skips_and_launches \
  test_spawn_relaunch_runs_nothing \
  test_spawn_secondmate_runs_nothing \
  test_spawn_refusal_runs_nothing \
  test_spawn_memory_gate \
  test_spawn_memory_gate_handles_dash_task_ids \
  test_direct_saved_exchange_and_feedback \
  test_direct_steer_ignores_the_launch_check \
  test_direct_saved_check_summary_comes_from_the_record \
  test_direct_list_answers_become_points \
  test_direct_absent_recall_has_no_expectation_questions \
  test_direct_lists_exchange_misquotes \
  test_direct_checks_card_quotes_against_the_card \
  test_direct_reports_flags \
  test_direct_ask_is_his_quoted_messages \
  test_direct_ask_without_quotes_is_his_last_messages \
  test_direct_unquoted_text_gets_no_intent_or_honcho_questions \
  test_drain_shows_a_sends_flags_once_without_a_wake \
  test_drain_shows_nothing_for_a_send_with_no_flag \
  test_direct_brief_with_many_headings_finishes \
  test_direct_key_from_the_named_file \
  test_direct_hides_only_unlocated_intent_flags \
  test_direct_read_error_prints_the_message \
  test_direct_failures_skip_without_the_key \
  test_direct_key_in_the_text_is_not_sent \
  test_direct_refuses_a_redirect \
  test_direct_no_session_log_skips
