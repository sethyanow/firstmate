#!/usr/bin/env bash
# The brief check at its two call sites and on its own.
#
# The call sites run against fake tmux worlds with no key and no model, so no
# network is touched. The direct runs point the check at a local stub through
# --questions, with a fake honcho, so the wire behavior is real HTTP.
set -u

# shellcheck source=tests/fixtures.sh
. "$(dirname "${BASH_SOURCE[0]}")/fixtures.sh"

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
        if self.path == "/ok":
            questions = json.loads(body)["questions"]
            answers = {}
            for qid, q in questions.items():
                if q["type"] == "noul":
                    answers[qid] = {"type": "noul", "noul": 0.9}
                else:
                    options = list(q["criteria"])
                    probabilities = {o: 1 / len(options) for o in options}
                    answers[qid] = {"type": "choice", "choice": options[0], "probabilities": probabilities, "confidence": 0.5}
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
{"type":"user","origin":{"kind":"human"},"message":{"content":"[fm-from-firstmate]corr=ab a marked message"}}
{"type":"user","message":{"content":"a summary record with no origin"}}
{"type":"assistant","message":{"content":[{"type":"text","text":"Shall I go ahead?"}]}}
JSONL
  printf '# Task\n## Captain'\''s intent\nthe intent line\n\n## Firstmate spec\nBuild the small thing.\n' > "$DIRECT_DIR/brief.md"
}

run_direct() {  # <check args...>
  env -u OPENROUTER_API_KEY PATH="$DIRECT_DIR/bin:$PATH" HOME="$DIRECT_DIR/user-home" FM_HOME="$DIRECT_DIR/home" \
    CLAUDE_CODE_SESSION_ID=session-1 python3 "$CHECK" --questions "$DIRECT_DIR/questions.toml" "$@" 2>&1
}

# The same run with whatever key the caller put in the environment.
run_direct_env() {
  env PATH="$DIRECT_DIR/bin:$PATH" HOME="$DIRECT_DIR/user-home" FM_HOME="$DIRECT_DIR/home" \
    CLAUDE_CODE_SESSION_ID=session-1 python3 "$CHECK" --questions "$DIRECT_DIR/questions.toml" "$@" 2>&1
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

test_direct_reports_flags_and_builds_the_ask() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world flags ok
  local out status request
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct_env --text "$DIRECT_DIR/brief.md")
  status=$?
  expect_code 0 "$status" "a run with flags should exit 0: $out"
  assert_contains "$out" 'brief-check send: ' "the sender should see the result: $out"
  assert_contains "$out" 'jev ' "the result should name the model"
  assert_contains "$out" '1234 tokens' "the result should show the token count"
  request=$(last_request)
  assert_equals true "$(printf '%s' "$request" | jq -r .key_sent)" "the key should go as the request header"
  assert_equals "$(printf 'the intent line\n\nfirst typed\n\nqueued words\n\nattached words')" \
    "$(printf '%s' "$request" | jq -r '.body | fromjson | .state.ask')" \
    "the ask should be the intent section and his last three typed messages"
  grep -q '"input_tokens": 1234' "$DIRECT_DIR"/home/state/brief-check/*-send.json \
    || fail "the log should hold the token count"
  assert_no_key_anywhere "$out"
  pass "direct: flags reach the sender, and the ask comes from the intent and his typed words"
}

test_direct_key_from_the_named_file() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world named-file ok
  local out
  printf 'OTHER=1\nexport OPENROUTER_API_KEY="%s"\n' "$FAKE_KEY" > "$DIRECT_DIR/user-home/keys.env"
  # shellcheck disable=SC2088
  printf '~/keys.env\nany later line is ignored\n' > "$DIRECT_DIR/home/config/brief-check"
  out=$(run_direct --text "$DIRECT_DIR/brief.md")
  assert_contains "$out" 'brief-check send: ' "the named file should supply the key: $out"
  assert_equals true "$(last_request | jq -r .key_sent)" "the key from the file should go as the request header"
  assert_no_key_anywhere "$out"
  pass "direct: the key can come from the file the flag names"
}

test_direct_routing_follows_the_registry() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world routing ok
  local out
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct_env --text "$DIRECT_DIR/brief.md" --to some-task)
  assert_equals false "$(last_request | jq -r '.body | fromjson | .questions | has("owner")')" \
    "with no registry the routing question should be left out: $out"
  printf -- '- game-mate - The game. (scope: the game project)\n' > "$DIRECT_DIR/home/data/secondmates.md"
  printf 'this-mate\nsecond line\n' > "$DIRECT_DIR/home/.fm-secondmate-home"
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct_env --text "$DIRECT_DIR/brief.md" --to some-task)
  assert_equals true "$(last_request | jq -r '.body | fromjson | .questions | has("owner")')" \
    "with a registry the routing question should be asked: $out"
  assert_equals this-mate "$(last_request | jq -r '.body | fromjson | .state.routing.handled_by')" \
    "the sender should default to the home's mate name"
  pass "direct: the routing question follows the home's registry and sender"
}

test_direct_failures_skip_without_the_key() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  local out status path
  for path in echo malformed; do
    new_direct_world "fail-$path" "$path"
    out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct_env --text "$DIRECT_DIR/brief.md")
    status=$?
    expect_code 0 "$status" "a failed answer ($path) should exit 0: $out"
    assert_equals 1 "$(skip_lines "$out")" "a failed answer ($path) should print one skip line: $out"
    assert_equals 1 "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" "a failed answer ($path) should print nothing else: $out"
    assert_no_key_anywhere "$out"
  done
  new_direct_world fail-honcho ok
  out=$(FM_TEST_HONCHO_FAIL=1 OPENROUTER_API_KEY="$FAKE_KEY" run_direct_env --text "$DIRECT_DIR/brief.md")
  status=$?
  expect_code 0 "$status" "a failed Honcho call should exit 0: $out"
  assert_equals 1 "$(skip_lines "$out")" "a failed Honcho call should print one skip line: $out"
  pass "direct: a failed answer prints one skip line and never the key"
}

test_direct_refuses_a_redirect() {
  have_tomllib || { pass "# skip direct run: python3 has no tomllib"; return; }
  start_stub
  new_direct_world redirect redirect
  local out status
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" run_direct_env --text "$DIRECT_DIR/brief.md")
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
  out=$(OPENROUTER_API_KEY="$FAKE_KEY" CLAUDE_CODE_SESSION_ID=unknown-session \
    env PATH="$DIRECT_DIR/bin:$PATH" HOME="$DIRECT_DIR/user-home" FM_HOME="$DIRECT_DIR/home" \
    python3 "$CHECK" --questions "$DIRECT_DIR/questions.toml" --text "$DIRECT_DIR/brief.md" 2>&1)
  status=$?
  expect_code 0 "$status" "no session log should exit 0: $out"
  assert_equals "${SKIP_PREFIX}no session log" "$out" "no session log should print one skip line"
  pass "direct: no session log prints one skip line"
}

fm_test_run \
  test_send_no_flag_runs_nothing \
  test_send_flag_without_key_skips_and_delivers \
  test_send_check_failure_keeps_delivery \
  test_send_exclusions \
  test_spawn_no_flag_runs_nothing \
  test_spawn_flag_without_key_skips_and_launches \
  test_spawn_relaunch_runs_nothing \
  test_spawn_secondmate_runs_nothing \
  test_spawn_refusal_runs_nothing \
  test_direct_reports_flags_and_builds_the_ask \
  test_direct_key_from_the_named_file \
  test_direct_routing_follows_the_registry \
  test_direct_failures_skip_without_the_key \
  test_direct_refuses_a_redirect \
  test_direct_no_session_log_skips
